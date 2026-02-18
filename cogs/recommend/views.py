# cogs/recommend/views.py

import discord
from discord import ui
import random

from . import db
from . import utils
from config import TEST_ROLE_ID

class GachaControlView(ui.View):
    def __init__(self, guild_forums: list[discord.ForumChannel]):
        super().__init__(timeout=180)
        self.selected_channel_id = None

        options = [discord.SelectOption(label="🌐 全部分区 (默认)", value="all")]
        options.extend([discord.SelectOption(label=f"📂 {forum.name}", value=str(forum.id)) for forum in guild_forums[:24]])

        self.channel_select = ui.Select(placeholder="[可选] 筛选特定资源池...", options=options, min_values=1, max_values=1)
        self.channel_select.callback = self.on_select_change
        self.add_item(self.channel_select)

    async def on_select_change(self, interaction: discord.Interaction):
        val = self.channel_select.values[0]
        self.selected_channel_id = int(val) if val != "all" else None

        pool_name = "全部分区"
        if self.selected_channel_id:
            ch = interaction.guild.get_channel(self.selected_channel_id)
            pool_name = ch.name if ch else "未知分区"

        await interaction.response.edit_message(content=f"🎯 卡池锁定：**{pool_name}**\n请点击下方按钮开始。", view=self)

    async def execute_draw(self, interaction: discord.Interaction, count: int):
        is_tester = isinstance(interaction.user, discord.Member) and bool(interaction.user.get_role(TEST_ROLE_ID))

        if not is_tester and await db.check_user_drawn_today(interaction.user.id):
            return await interaction.response.send_message("🔮 您今天已经感应过缘分啦，明天再来吧！", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        threads = await utils.get_random_thread_pool(interaction.guild, self.selected_channel_id)
        if not threads:
            return await interaction.followup.send("🏜️ 卡池里空空如也...", ephemeral=True)

        drawn_threads = random.sample(threads, min(count, len(threads)))

        if not is_tester:
            await db.mark_user_drawn(interaction.user.id)

        embeds = []
        footer_text = f"点击标题跳转详情{' (测试模式)' if is_tester else ''}"

        if len(drawn_threads) == 1:
            info = await utils.fetch_thread_details(drawn_threads[0])
            embed = discord.Embed(title=f"✨ {info['title']}", description=f"👤 {info['author_mention']}\n\n{info['intro']}", color=0xffd700, url=info['url'])
            embed.set_author(name=info['author_name'], icon_url=info['author_avatar'])
            embed.add_field(name="📂 分区", value=info['category'], inline=True).add_field(name="🏷️ 标签", value=" / ".join(info['tags']), inline=True)
            if info['image']: embed.set_image(url=info['image'])
            embed.set_footer(text=f"今日缘分已定！{' (测试员模式)' if is_tester else ''}")
            embeds.append(embed)
        else:
            desc = "\n".join([f"{i+1}. **[{t.name}]({t.jump_url})**" for i, t in enumerate(drawn_threads)])
            embeds.append(discord.Embed(title=f"💫 恭喜获得 {len(drawn_threads)} 连抽结果！", description=desc, color=0xff69b4, footer=footer_text))

        await interaction.followup.send(embeds=embeds, ephemeral=True)

    @ui.button(label="单抽", style=discord.ButtonStyle.primary, emoji="1️⃣")
    async def draw_one(self, i: discord.Interaction, b: ui.Button): await self.execute_draw(i, 1)

    @ui.button(label="五连", style=discord.ButtonStyle.secondary, emoji="5️⃣")
    async def draw_five(self, i: discord.Interaction, b: ui.Button): await self.execute_draw(i, 5)

    @ui.button(label="十连", style=discord.ButtonStyle.success, emoji="🔟")
    async def draw_ten(self, i: discord.Interaction, b: ui.Button): await self.execute_draw(i, 10)


class DailyRecommendView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="🔮 抽取今日缘分", style=discord.ButtonStyle.primary, custom_id="daily_gacha_open_btn")
    async def open_gacha(self, interaction: discord.Interaction, button: ui.Button):
        forums = utils.get_card_forums(interaction.guild)
        if not forums:
            return await interaction.response.send_message("本服未配置资源频道。", ephemeral=True)

        view = GachaControlView(forums)
        await interaction.response.send_message("🎴 **抽卡控制台**\n请选择卡池，然后点击抽卡。", view=view, ephemeral=True)