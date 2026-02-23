# cogs/statistics/cog.py

import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import time, datetime
import asyncio
import random

from . import db
from . import utils
from .views import StatisticsContainerView, ForumSelectView
from config import TZ_SHANGHAI, RECOMMEND_TARGET_KEYWORDS

class StatisticsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # 在 Cog 加载时初始化数据库并启动后台任务
        self.bot.loop.create_task(db.init_statistics_db())
        self.daily_refresh_task.start()
        # === 新增：启动顶帖任务 ===
        self.auto_bump_archived_task.start()

    def cog_unload(self):
        """当 Cog 被卸载时，取消后台任务"""
        self.daily_refresh_task.cancel()
        # === 新增：取消顶帖任务 ===
        self.auto_bump_archived_task.cancel()

    @app_commands.command(name="生成统计面板", description="[管理员] 为指定的论坛频道创建一份数据统计报告")
    @app_commands.default_permissions(administrator=True)
    async def create_stats_panel(self, interaction: discord.Interaction):
        """
        发送一个带有频道选择器的消息，让用户选择要统计的论坛。
        """
        view = ForumSelectView(self.bot)
        await interaction.response.send_message(
            "请在下方选择你想要生成统计报告的论坛频道，然后点击按钮确认。",
            view=view,
            ephemeral=True
        )

    @tasks.loop(time=time(hour=0, minute=0, tzinfo=TZ_SHANGHAI))
    async def daily_refresh_task(self):
        """每日零点自动刷新所有已记录的统计面板"""
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
        """在任务循环开始前，等待 bot 完全就绪"""
        await self.bot.wait_until_ready()

    # === 修改核心区域：顶帖任务逻辑 ===
    @tasks.loop(hours=3)
    async def auto_bump_archived_task(self):
        """每3小时扫描指定频道，智能地顶起一批已归档的帖子"""
        print(f"\n[{datetime.now(TZ_SHANGHAI)}] 🚀 开始执行智能顶帖任务...")

        # 定义每次顶帖的数量，可以设为一个范围，然后随机取值
        BUMP_COUNT = random.randint(5, 10)

        # 遍历机器人所在的所有服务器
        for guild in self.bot.guilds:
            if not guild.me.guild_permissions.manage_threads:
                print(f"  - 权限不足: 在服务器 {guild.name} 中缺少'管理帖子'权限，跳过。")
                continue

            # 1. 找出服务器内所有符合关键词的论坛频道
            target_forums = [
                f for f in guild.forums
                if any(keyword in f.name for keyword in RECOMMEND_TARGET_KEYWORDS)
            ]
            if not target_forums:
                continue

            print(f"  - 正在扫描服务器: {guild.name}")

            # 2. 从这些频道中获取所有已归档的帖子
            archived_threads = []
            for forum in target_forums:
                try:
                    async for thread in forum.archived_threads(limit=100): # 限制单次获取上限，防止超时
                        archived_threads.append(thread)
                except discord.Forbidden:
                    print(f"    - 权限不足, 无法读取频道 {forum.name} 的归档帖子。")
                except Exception as e:
                    print(f"    - 读取频道 {forum.name} 归档时出错: {e}")

            if not archived_threads:
                print(f"    - 服务器 {guild.name} 没有找到需要顶的帖子。")
                continue

            # 3. 智能选择一批帖子来顶
            thread_with_bump_time = []
            for thread in archived_threads:
                last_bumped = await db.get_last_bumped_time(thread.id)
                if last_bumped is None:
                    last_bumped = datetime(2000, 1, 1, tzinfo=TZ_SHANGHAI)
                thread_with_bump_time.append((thread, last_bumped))

            # 按时间正序排序，最久没被顶的在最前面
            thread_with_bump_time.sort(key=lambda x: x[1])

            # 选择最优先的一批帖子，数量由 BUMP_COUNT 决定
            threads_to_bump = [item[0] for item in thread_with_bump_time[:BUMP_COUNT]]

            if not threads_to_bump:
                print(f"    - 没有符合条件的帖子可供顶帖。")
                continue

            print(f"    - 计划在本轮顶起 {len(threads_to_bump)} 个帖子。")

            # 4. 遍历并执行顶帖操作
            bumped_count_this_run = 0
            for thread_to_bump in threads_to_bump:
                try:
                    # 使用 unarchive() 效率更高且更稳定
                    await thread_to_bump.unarchive(reason="自动顶帖")
                    await db.record_bump(thread_to_bump.id, guild.id)
                    bumped_count_this_run += 1

                    print(f"      - ✅ 成功顶帖: '{thread_to_bump.name}' (ID: {thread_to_bump.id})")
                    await asyncio.sleep(2) # 每次顶帖之间增加短暂延迟

                except discord.Forbidden:
                    print(f"      - ❌ 顶帖失败: 没有权限操作帖子 '{thread_to_bump.name}'。")
                except Exception as e:
                    print(f"      - ❌ 顶帖时发生未知错误: {e}")

            print(f"    - 本轮在 {guild.name} 共成功顶起 {bumped_count_this_run} / {len(threads_to_bump)} 个帖子。")
            await asyncio.sleep(10) # 每个服务器之间增加延迟

        print(f"[{datetime.now(TZ_SHANGHAI)}] ✨ 智能顶帖任务本轮执行完毕。")

    @auto_bump_archived_task.before_loop
    async def before_auto_bump_task(self):
        """在顶帖任务开始前等待 Bot 就绪"""
        await self.bot.wait_until_ready()
        print("智能顶帖任务将在1分钟后开始第一次运行...")
        await asyncio.sleep(60)