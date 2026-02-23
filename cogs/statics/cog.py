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
        # ... (此部分代码无需改动，保持原样) ...
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
                target_forum = guild.get_channel(panel_record["target_forum_id"])
                if not isinstance(target_forum, discord.ForumChannel):
                    await db.remove_panel_record(panel_record["message_id"])
                    continue
                message = await panel_channel.fetch_message(panel_record["message_id"])
                new_stats = await utils.fetch_forum_stats(target_forum)
                new_view = StatisticsContainerView(target_forum, new_stats)
                await message.edit(view=new_view)
                refreshed_count += 1
                await asyncio.sleep(2)
            except discord.NotFound:
                await db.remove_panel_record(panel_record["message_id"])
                print(f"  - 面板消息 {panel_record['message_id']} 已被删除，从数据库移除。")
            except discord.Forbidden:
                print(f"  - ❌ 权限不足，无法编辑面板 {panel_record['message_id']}。")
            except Exception as e:
                print(f"  - ❌ 刷新面板 {panel_record['message_id']} 时发生未知错误: {e}")
        print(f"✅ 每日刷新任务完成，共成功刷新 {refreshed_count} / {len(all_panels)} 个面板。")


    @daily_refresh_task.before_loop
    async def before_daily_task(self):
        await self.bot.wait_until_ready()

    # === 修复顶帖任务 ===
    @tasks.loop(hours=3)
    async def auto_bump_archived_task(self):
        """每3小时扫描一次，通过发评论顶起“沉底”的帖子"""
        print(f"\n[{datetime.now(TZ_SHANGHAI)}] 🚀 开始执行【评论式】顶帖任务...")

        SILENT_DAYS = 3
        BUMP_MESSAGES = ["顶", "捞一下", "再捞捞", "顶顶", "顶起"]
        three_days_ago = datetime.now(TZ_SHANGHAI) - timedelta(days=SILENT_DAYS)

        for guild in self.bot.guilds:
            if not guild.me.guild_permissions.send_messages_in_threads:
                print(f"  - 权限不足: 在服务器 {guild.name} 中缺少'在帖子中发送消息'权限，跳过。")
                continue

            target_forums = [f for f in guild.forums if any(keyword in f.name for keyword in RECOMMEND_TARGET_KEYWORDS)]
            if not target_forums:
                continue

            print(f"  - 正在扫描服务器: {guild.name}")

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
                    # 正确地从异步生成器中获取最后一条消息
                    last_message = None
                    history = thread.history(limit=1)
                    # 使用 anext 从异步迭代器中安全地取出一项
                    last_message = await history.__anext__()

                    if last_message and last_message.created_at.astimezone(TZ_SHANGHAI) < three_days_ago:
                        if last_message.author != self.bot.user:
                             threads_to_bump.append(thread)

                except StopAsyncIteration:
                    # 帖子中没有任何消息，不是我们要找的目标
                    continue
                except discord.Forbidden:
                    # 没有权限访问帖子历史，跳过
                    continue
                except Exception as e:
                    print(f"    - 检查帖子 {thread.name} 时出错: {e}")


            if not threads_to_bump:
                print(f"    - 在 {guild.name} 中没有找到超过 {SILENT_DAYS} 天未回复的帖子。")
                continue

            print(f"    - 发现 {len(threads_to_bump)} 个沉底帖子，计划顶起。")

            bumped_count_this_run = 0
            random.shuffle(threads_to_bump)
            for thread_to_bump in threads_to_bump[:5]: # 每次最多顶5个
                try:
                    await thread_to_bump.send(random.choice(BUMP_MESSAGES))
                    bumped_count_this_run += 1
                    print(f"      - ✅ 成功顶帖: '{thread_to_bump.name}' (ID: {thread_to_bump.id})")
                    await asyncio.sleep(10)
                except discord.Forbidden:
                    print(f"      - ❌ 顶帖失败: 没有权限在帖子 '{thread_to_bump.name}' 中发言。")
                except Exception as e:
                    print(f"      - ❌ 顶帖时发生未知错误: {e}")

            print(f"    - 本轮在 {guild.name} 共成功顶起 {bumped_count_this_run} 个帖子。")
            await asyncio.sleep(10)

        print(f"[{datetime.now(TZ_SHANGHAI)}] ✨ 【评论式】顶帖任务本轮执行完毕。")

    @auto_bump_archived_task.before_loop
    async def before_auto_bump_task(self):
        await self.bot.wait_until_ready()
        print("评论式顶帖任务将在1分钟后开始第一次运行...")
        await asyncio.sleep(60)