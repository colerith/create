# cogs/recommend/cog.py

import discord
from discord import app_commands
from discord.ext import commands, tasks
import asyncio
import random
from datetime import datetime, time

from . import db
from . import utils
# 引入新的 Container View
from .views import DailyRecommendContainer

from config import TZ_SHANGHAI, RECOMMEND_DAILY_CHANNEL_IDS, TEST_ROLE_ID

class RecommendCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.loop.create_task(db.init_recommend_db())
        self.daily_recommend_task.start()

    async def cog_unload(self):
        self.daily_recommend_task.cancel()

    async def _cleanup_old_messages(self, channel: discord.TextChannel):
        """删除旧的推荐消息 (根据 Container 特征或作者 ID)"""
        try:
            async for msg in channel.history(limit=20):
                if msg.author.id == self.bot.user.id:
                    await msg.delete()
                    await asyncio.sleep(0.5)
        except Exception as e:
            print(f"清理推荐面板出错: {e}")

    async def refresh_recommendation_panel(self, channel: discord.TextChannel, mode: str = "edit"):
        """刷新推荐面板 (Container 版)"""
        pool = await utils.get_random_thread_pool(channel.guild)

        if not pool:
            if mode == "reset":
                await self._cleanup_old_messages(channel)
                # 空状态 Container
                view = DailyRecommendContainer({}, is_empty=True)
                await channel.send(view=view)
            return

        target_thread = random.choice(pool)
        info = await utils.fetch_thread_details(target_thread)

        # 创建新的 Container View
        view = DailyRecommendContainer(info)

        if mode == "reset":
            await self._cleanup_old_messages(channel)
            await channel.send(view=view)
        else: # "edit" mode
            try:
                # 尝试查找旧消息并编辑 (Container消息编辑就是替换view)
                found = False
                async for msg in channel.history(limit=20):
                    if msg.author.id == self.bot.user.id:
                        # 假设最近一条 Bot 消息就是面板
                        await msg.edit(view=view) # Container 更新不需要 embed, 只换 view
                        found = True
                        break

                if not found:
                    await channel.send(view=view)
            except Exception as e:
                print(f"每日推荐面板刷新失败: {e}")
                # 失败兜底：发新的
                await channel.send(view=view)

    @tasks.loop(time=time(hour=0, minute=0, tzinfo=TZ_SHANGHAI))
    async def daily_recommend_task(self):
        for channel_id in RECOMMEND_DAILY_CHANNEL_IDS:
            if channel := self.bot.get_channel(channel_id):
                await self.refresh_recommendation_panel(channel, mode="reset") # 每日0点强制重发，保持最新
                await asyncio.sleep(2)

    @daily_recommend_task.before_loop
    async def before_daily_task(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="更新推荐面板", description="[管理] 强制刷新并重发今日推荐")
    @app_commands.default_permissions(manage_guild=True)
    async def manual_recommend(self, interaction: discord.Interaction):
        is_admin = interaction.user.guild_permissions.administrator
        is_tester = isinstance(interaction.user, discord.Member) and bool(interaction.user.get_role(TEST_ROLE_ID))
        if not (is_admin or is_tester):
            return await interaction.response.send_message("仅限管理员或测试员使用。", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        # 强制重置模式
        await self.refresh_recommendation_panel(interaction.channel, mode="reset")
        await interaction.followup.send("✅ 推荐面板已强制刷新 (Container版)！", ephemeral=True)
