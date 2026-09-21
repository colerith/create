import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
from discord.ext import commands

from cogs.recommend.cog import RecommendCog
from cogs.recommend.views import DailyRecommendContainer, GachaContainerView


class RecommendationTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_daily_panel_callback_registered_on_load(self):
        bot = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        async with bot:
            cog = RecommendCog(bot)
            try:
                with patch('cogs.recommend.cog.db.init_recommend_db', AsyncMock()) as init:
                    await cog.cog_load()
                    init.assert_awaited_once()
                self.assertIn(cog.persistent_view, bot.persistent_views)
                buttons = {item.custom_id: item for item in cog.persistent_view.walk_children() if isinstance(item, discord.ui.Button)}
                interaction = SimpleNamespace(guild=object(), response=SimpleNamespace(send_message=AsyncMock()))
                with patch('cogs.recommend.views.utils.get_card_forums', return_value=[]):
                    await buttons['daily_gacha_open_btn'].callback(interaction)
                interaction.response.send_message.assert_awaited_once_with('本服未配置资源频道。', ephemeral=True)
            finally:
                await cog.cog_unload()
            self.assertNotIn(cog.persistent_view, bot.persistent_views)

    async def test_draw_acknowledges_before_database_check(self):
        user = SimpleNamespace(id=1, display_avatar=SimpleNamespace(url='https://example.com/avatar.png'))
        view = GachaContainerView([], user)
        interaction = SimpleNamespace(user=user, response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
        async def checked(user_id):
            interaction.response.defer.assert_awaited_once()
            return True
        with patch('cogs.recommend.views.db.check_user_drawn_today', side_effect=checked):
            await view.execute_draw(interaction, 1)
        interaction.followup.send.assert_awaited_once()

    async def test_failed_result_does_not_consume_daily_draw(self):
        user = SimpleNamespace(id=1, display_avatar=SimpleNamespace(url='https://example.com/avatar.png'))
        view = GachaContainerView([], user)
        interaction = SimpleNamespace(user=user, guild=object(), response=SimpleNamespace(defer=AsyncMock()), edit_original_response=AsyncMock(side_effect=RuntimeError('send failed')))
        details = dict(title='测试', url='https://example.com/thread', author_mention='作者', category='分类', intro='简介')
        with patch('cogs.recommend.views.db.check_user_drawn_today', AsyncMock(return_value=False)), patch('cogs.recommend.views.utils.get_random_thread_pool', AsyncMock(return_value=[object()])), patch('cogs.recommend.views.utils.fetch_thread_details', AsyncMock(return_value=details)), patch('cogs.recommend.views.db.mark_user_drawn', AsyncMock()) as mark:
            with self.assertRaisesRegex(RuntimeError, 'send failed'):
                await view.execute_draw(interaction, 1)
            mark.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
