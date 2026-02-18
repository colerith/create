# cogs/recommend/cog.py

import discord
from discord import app_commands
from discord.ext import commands, tasks
import asyncio
import random
from datetime import datetime, time

from . import db
from . import utils
from .views import DailyRecommendView

from config import TZ_SHANGHAI, RECOMMEND_DAILY_CHANNEL_IDS, TEST_ROLE_ID

class RecommendCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # 视图是持久的，在bot启动时就添加
        self.bot.add_view(DailyRecommendView())
        # 在后台初始化数据库表
        self.bot.loop.create_task(db.init_recommend_db())
        # 启动后台定时任务
        self.daily_recommend_task.start()

    async def cog_unload(self):
        """Cog卸载时取消后台任务"""
        self.daily_recommend_task.cancel()

    async def _cleanup_old_messages(self, channel: discord.TextChannel):
        """删除旧的推荐消息"""
        try:
            async for msg in channel.history(limit=20):
                if msg.author.id == self.bot.user.id and msg.embeds and "每日精选" in msg.embeds[0].title:
                    await msg.delete(); await asyncio.sleep(0.5)
        except Exception as e:
            print(f"清理推荐面板出错: {e}")

    async def refresh_recommendation_panel(self, channel: discord.TextChannel, mode: str = "edit"):
        """核心刷新逻辑 (mode: 'edit' 或 'reset')"""
        pool = await utils.get_random_thread_pool(channel.guild)
        if not pool:
            if mode == "reset":
                await self._cleanup_old_messages(channel)
                await channel.send(embed=discord.Embed(title="📅 每日推荐", description="今天资源库里空空如也...", color=0x99aab5))
            return

        target_thread = random.choice(pool)
        info = await utils.fetch_thread_details(target_thread)
        date_str = datetime.now(TZ_SHANGHAI).strftime("%m月%d日")

        embed = discord.Embed(
            title=f"📅 {date_str} · 每日精选",
            description=f"### [{info['title']}]({info['url']})\n👤 作者: {info['author_mention']}\n\n{info['intro']}",
            color=0xff69b4
        )
        embed.set_author(name=info['author_name'], icon_url=info['author_avatar'])
        embed.add_field(name="📂 分区", value=info['category'], inline=True).add_field(name="🏷️ 标签", value=" / ".join(info['tags']), inline=True)
        if info['image']: embed.set_image(url=info['image'])
        embed.set_footer(text="点击下方按钮抽取你的今日缘分！(每日限一次)")

        view = DailyRecommendView() # 每次都用新的，以防旧的超时

        if mode == "reset":
            await self._cleanup_old_messages(channel)
            await channel.send(embed=embed, view=view)
        else: # "edit" mode
            try:
                # 尝试查找并编辑
                async for msg in channel.history(limit=20):
                    if msg.author.id == self.bot.user.id and msg.embeds and "每日精选" in msg.embeds[0].title:
                        await msg.edit(embed=embed, view=view)
                        return
                # 没找到就发送新的
                await channel.send(embed=embed, view=view)
            except Exception as e:
                print(f"每日推荐面板刷新失败: {e}")

    @tasks.loop(time=time(hour=0, minute=0, tzinfo=TZ_SHANGHAI))
    async def daily_recommend_task(self):
        """每天0点自动刷新所有配置的频道"""
        for channel_id in RECOMMEND_DAILY_CHANNEL_IDS:
            if channel := self.bot.get_channel(channel_id):
                await self.refresh_recommendation_panel(channel, mode="edit")
                await asyncio.sleep(2) # 简单的速率保护

    @daily_recommend_task.before_loop
    async def before_daily_task(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="更新推荐面板", description="[管理] 强制刷新并重发今日推荐")
    @app_commands.default_permissions(manage_guild=True)
    async def manual_recommend(self, interaction: discord.Interaction):
        # 权限检查
        is_admin = interaction.user.guild_permissions.administrator
        is_tester = isinstance(interaction.user, discord.Member) and bool(interaction.user.get_role(TEST_ROLE_ID))
        if not (is_admin or is_tester):
            return await interaction.response.send_message("仅限管理员或测试员使用。", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        await self.refresh_recommendation_panel(interaction.channel, mode="reset")
        await interaction.followup.send("✅ 推荐面板已强制刷新！", ephemeral=True)