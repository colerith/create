# cogs/recommend/cog.py

import discord
from discord import app_commands
from discord.ext import commands, tasks
import asyncio
import random
from datetime import datetime, time

from . import db
from . import utils
from .views import DailyRecommendContainer

from config import TZ_SHANGHAI, RECOMMEND_DAILY_CHANNEL_IDS, TEST_ROLE_ID

class RecommendCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.loop.create_task(db.init_recommend_db())
        self.daily_recommend_task.start()

    async def cog_unload(self):
        self.daily_recommend_task.cancel()

    async def refresh_recommendation_panel(self, channel: discord.TextChannel):
        """
        刷新推荐面板 (Container 版) - 智能编辑模式
        """
        pool = await utils.get_random_thread_pool(channel.guild)

        # 准备 View
        if not pool:
            # 空状态
            view = DailyRecommendContainer({}, is_empty=True)
        else:
            target_thread = random.choice(pool)
            info = await utils.fetch_thread_details(target_thread)
            view = DailyRecommendContainer(info)

        # 尝试查找并编辑旧面板
        target_msg = None
        try:
            # 扫描最近 20 条消息
            async for msg in channel.history(limit=20):
                if msg.author == self.bot.user:
                    if msg.components:
                        is_panel = False
                        for action_row in msg.components:
                            for child in action_row.children:
                                if getattr(child, "custom_id", "") == "daily_gacha_open_btn":
                                    is_panel = True
                                    break
                            if is_panel: break

                        if is_panel:
                            target_msg = msg
                            break
        except Exception as e:
            print(f"扫描推荐面板历史出错: {e}")

        # 执行更新或发送
        if target_msg:
            try:
                # 编辑已有消息
                await target_msg.edit(view=view)
                return
            except discord.NotFound:
                pass # 消息可能被删了，这就发新的
            except Exception as e:
                print(f"编辑推荐面板失败: {e}")

        # 如果没找到或编辑失败，发送新的
        # 先尝试删除旧的（如果判定为旧面板但编辑失败的）以防堆积，但不大范围清除
        if target_msg:
            try: await target_msg.delete()
            except: pass

        await channel.send(view=view)

    @tasks.loop(time=time(hour=0, minute=0, tzinfo=TZ_SHANGHAI))
    async def daily_recommend_task(self):
        for channel_id in RECOMMEND_DAILY_CHANNEL_IDS:
            if channel := self.bot.get_channel(channel_id):
                # 每日任务也使用 refresh 逻辑，复用编辑
                await self.refresh_recommendation_panel(channel)
                await asyncio.sleep(2)

    @daily_recommend_task.before_loop
    async def before_daily_task(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="更新推荐面板", description="[管理] 刷新本频道的今日推荐内容")
    @app_commands.default_permissions(manage_guild=True)
    async def manual_recommend(self, interaction: discord.Interaction):
        is_admin = interaction.user.guild_permissions.administrator
        is_tester = isinstance(interaction.user, discord.Member) and bool(interaction.user.get_role(TEST_ROLE_ID))
        if not (is_admin or is_tester):
            return await interaction.response.send_message("仅限管理员或测试员使用。", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        # 调用复用的刷新逻辑
        await self.refresh_recommendation_panel(interaction.channel)
        await interaction.followup.send("✅ 推荐面板内容已刷新！", ephemeral=True)