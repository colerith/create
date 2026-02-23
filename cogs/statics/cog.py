# cogs/statistics/cog.py

import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import time, datetime, timedelta
import asyncio
import random

from . import db
from . import utils
from .views import StatisticsContainerView, ForumSelectView
from config import TZ_SHANGHAI, RECOMMEND_TARGET_KEYWORDS

class StatisticsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.loop.create_task(db.init_statistics_db())
        self.daily_refresh_task.start()
        self.auto_bump_archived_task.start()

    async def cog_load(self):
        self.bot.loop.create_task(self.register_persistent_views_after_ready())

    async def register_persistent_views_after_ready(self):
        await self.bot.wait_until_ready()
        print("📊 [StatisticsCog] Bot 已就绪，开始注册持久化统计面板...")
        all_panels = await db.get_all_panels()
        registered_forums = set()
        count = 0
        for panel in all_panels:
            forum_id = panel['target_forum_id']
            if forum_id not in registered_forums:
                view_instance = StatisticsContainerView(self.bot, forum_id)
                self.bot.add_view(view_instance)
                registered_forums.add(forum_id)
                count += 1
        print(f"✅ [StatisticsCog] 共为 {count} 个不同的论坛注册了刷新按钮。")

    def cog_unload(self):
        self.daily_refresh_task.cancel()
        self.auto_bump_archived_task.cancel()

    @app_commands.command(name="生成统计面板", description="[管理员] 为指定的论坛频道创建一份数据统计报告")
    @app_commands.default_permissions(administrator=True)
    async def create_stats_panel(self, interaction: discord.Interaction):
        if not interaction.guild:
             return await interaction.response.send_message("此命令必须在服务器内使用。", ephemeral=True)
        view = ForumSelectView(self.bot)
        await interaction.response.send_message(
            "请在下方选择你想要生成统计报告的论坛频道，然后点击按钮确认。",
            view=view,
            ephemeral=True
        )

    @tasks.loop(time=time(hour=0, minute=0, tzinfo=TZ_SHANGHAI))
    async def daily_refresh_task(self):
        print(f"[{datetime.now(TZ_SHANGHAI)}] 🌞 开始执行每日统计面板刷新任务...")
        all_panels = await db.get_all_panels()
        if not all_panels:
            print("  - 数据库中没有需要刷新的面板，任务结束。")
            return

        refreshed_count = 0
        for panel_record in all_panels:
            try:
                guild = self.bot.get_guild(panel_record["guild_id"])
                if not guild: continue
                panel_channel = guild.get_channel(panel_record["channel_id"])
                if not panel_channel: continue

                target_forum_id = panel_record["target_forum_id"]
                message = await panel_channel.fetch_message(panel_record["message_id"])

                # 创建一个新的视图实例并调用刷新方法
                view_instance = StatisticsContainerView(self.bot, target_forum_id)
                await view_instance.refresh_data_and_update(message_to_edit=message)

                refreshed_count += 1
                await asyncio.sleep(2)
            except discord.NotFound:
                await db.remove_panel_record(panel_record["message_id"])
            except discord.Forbidden:
                pass
            except Exception as e:
                import traceback
                traceback.print_exc()

        print(f"✅ 每日刷新任务完成，共成功刷新 {refreshed_count} / {len(all_panels)} 个面板。")

    @daily_refresh_task.before_loop
    async def before_daily_task(self):
        await self.bot.wait_until_ready()

    # --- 【修复】顶帖任务 ---
    @tasks.loop(hours=3)
    async def auto_bump_archived_task(self):
        """每3小时扫描一次，通过发评论（并立即删除）来顶起“沉底”的帖子"""
        print(f"\n[{datetime.now(TZ_SHANGHAI)}] 🚀 开始执行【无痕】顶帖任务...")

        SILENT_DAYS = 3
        BUMP_MESSAGES = ["✨发现了一个宝藏好帖唷呐！"]
        three_days_ago = datetime.now(TZ_SHANGHAI) - timedelta(days=SILENT_DAYS)

        for guild in self.bot.guilds:
            if not guild.me.guild_permissions.send_messages_in_threads or not guild.me.guild_permissions.manage_messages:
                # 同时需要发送和删除消息的权限
                continue

            target_forums = [f for f in guild.forums if any(keyword in f.name for keyword in RECOMMEND_TARGET_KEYWORDS)]
            if not target_forums:
                continue

            threads_to_bump = []
            all_threads = []
            for forum in target_forums:
                all_threads.extend(forum.threads)
                try:
                    async for thread in forum.archived_threads(limit=1000):
                        all_threads.append(thread)
                except discord.Forbidden:
                    pass

            for thread in all_threads:
                try:
                    last_message = await thread.history(limit=1).__anext__()
                    if last_message and last_message.created_at.astimezone(TZ_SHANGHAI) < three_days_ago:
                        if last_message.author != self.bot.user:
                             threads_to_bump.append(thread)
                except:
                    continue

            if not threads_to_bump:
                continue

            random.shuffle(threads_to_bump)
            for thread_to_bump in threads_to_bump[:5]:
                try:
                    # 发送消息并获得消息对象
                    bump_msg = await thread_to_bump.send(random.choice(BUMP_MESSAGES))
                    # 短暂等待，确保顶帖效果生效
                    await asyncio.sleep(5)
                    # 删除刚刚发送的消息
                    await bump_msg.delete()
                    print(f"      - ✅ 成功无痕顶帖: '{thread_to_bump.name}' (ID: {thread_to_bump.id})")
                    # 在每个操作后也增加延时，防止速率超限
                    await asyncio.sleep(10)
                except Exception as e:
                    print(f"      - ❌ 无痕顶帖时发生错误: {e}")

        print(f"[{datetime.now(TZ_SHANGHAI)}] ✨ 【无痕】顶帖任务本轮执行完毕。")

    @auto_bump_archived_task.before_loop
    async def before_auto_bump_task(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(60)