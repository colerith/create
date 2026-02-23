import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timedelta, timezone, time
import asyncio
import random

from . import db as statistics_db
from .views import ForumSelectView, StatisticsContainerView
from config import TZ_SHANGHAI, RECOMMEND_TARGET_KEYWORDS

class StatisticsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # 启动新的任务
        self.daily_stats_refresh.start()
        self.bumping_task.start()

    async def cog_load(self):
        # 确保数据库表已创建
        await statistics_db.init_statistics_db()
        print("✅ [StatisticsCog] 已成功加载。")

    async def cog_unload(self):
        # 停止新的任务
        self.daily_stats_refresh.cancel()
        self.bumping_task.cancel()

    async def gather_statistics(self, forum: discord.ForumChannel):
        """
        收集单个论坛的统计数据。
        """
        if not forum:
            return None

        # 仅获取未归档的帖子
        threads = [t for t in forum.threads if not t.archived]

        if not threads:
            return {
                "channel_name": forum.name, "channel_icon_url": forum.guild.icon.url if forum.guild.icon else None,
                "total_threads": 0, "new_threads_7d": 0, "hot_threads": [], "cold_threads": []
            }

        seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)
        total_threads = len(threads)
        new_threads_7d = sum(1 for t in threads if t.created_at > seven_days_ago)

        threads_with_stats = []
        for t in threads:
            starter = t.starter_message
            if not starter:
                try:
                    history = [msg async for msg in t.history(limit=1, oldest_first=True)]
                    starter = history[0] if history else None
                except (discord.errors.Forbidden, IndexError):
                    starter = None

            likes = sum(r.count for r in starter.reactions) if starter else 0
            comments = t.message_count - 1 if t.message_count > 0 else 0
            author_name = t.owner.display_name if t.owner else "未知作者"
            tags = [tag.name for tag in t.applied_tags]

            threads_with_stats.append({
                "id": t.id, "name": t.name, "url": t.jump_url, "created_at": t.created_at,
                "likes": likes, "comments": comments, "score": likes * 1.5 + comments,
                "author_name": author_name, "tags": tags
            })

        threads_with_stats.sort(key=lambda x: x['score'], reverse=True)
        hot_threads = threads_with_stats[:15]

        now = datetime.now(timezone.utc)
        thirty_days_ago = now - timedelta(days=30)
        older_threads = [t for t in threads_with_stats if t['created_at'] < thirty_days_ago]
        older_threads.sort(key=lambda x: x['score'], reverse=False)
        cold_threads = older_threads[:15]

        return {
            "channel_name": forum.name, "channel_icon_url": forum.guild.icon.url if forum.guild.icon else None,
            "total_threads": total_threads, "new_threads_7d": new_threads_7d,
            "hot_threads": hot_threads, "cold_threads": cold_threads
        }

    # ==========================================
    # Part 1. 斜杠命令
    # ==========================================
    statistics_group = app_commands.Group(name="统计", description="统计面板相关命令")

    @statistics_group.command(name="发送选择器", description="[管理] 发送一个用于创建统计面板的频道选择器")
    @app_commands.default_permissions(manage_guild=True)
    async def send_selector(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "请在下方选择要为其生成统计面板的论坛频道：",
            view=ForumSelectView(self),
            ephemeral=True
        )

    @statistics_group.command(name="清理无效面板", description="[管理] 清理数据库中已在Discord被删除的面板记录")
    @app_commands.default_permissions(manage_guild=True)
    async def cleanup_panels(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        all_panels = await statistics_db.get_all_statistics_panels()
        if not all_panels:
            return await interaction.followup.send("数据库中没有面板记录，无需清理。", ephemeral=True)

        cleaned_count = 0
        for panel_data in all_panels:
            try:
                channel = self.bot.get_channel(panel_data['panel_channel_id'])
                if channel:
                    await channel.fetch_message(panel_data['message_id'])
                else:
                    raise discord.NotFound(None, "Channel not found")
            except discord.NotFound:
                await statistics_db.remove_statistics_panel(panel_data['message_id'])
                cleaned_count += 1

        await interaction.followup.send(f"✅ 清理完成！共移除了 **{cleaned_count}** 条无效的面板记录。", ephemeral=True)


    # ==========================================
    # Part 2. 后台任务
    # ==========================================
    @tasks.loop(time=time(hour=0, minute=0, tzinfo=TZ_SHANGHAI))
    async def daily_stats_refresh(self):
        print(f"[{datetime.now(TZ_SHANGHAI)}] 启动每日统计刷新任务...")
        panels = await statistics_db.get_all_statistics_panels()

        for panel_info in panels:
            try:
                guild = self.bot.get_guild(panel_info['guild_id'])
                if not guild: continue

                panel_channel = guild.get_channel(panel_info['panel_channel_id'])
                forum_channel = guild.get_channel(panel_info['forum_channel_id'])

                if not panel_channel or not forum_channel or not isinstance(forum_channel, discord.ForumChannel):
                    continue

                stats_data = await self.gather_statistics(forum_channel)
                if not stats_data: continue

                try:
                    msg = await panel_channel.fetch_message(panel_info['message_id'])
                    new_view = StatisticsContainerView(stats_data=stats_data)
                    await msg.edit(view=new_view)
                    print(f"- [成功] 已刷新版面 {msg.id} (关于 {forum_channel.name})")
                except discord.NotFound:
                    await statistics_db.remove_statistics_panel(panel_info['message_id'])
                    print(f"- [警告] 找不到统计面板消息 {panel_info['message_id']}，已从数据库移除。")
                except discord.Forbidden:
                    print(f"- [错误] 没有权限编辑消息 {panel_info['message_id']}。")
                except Exception as e:
                    print(f"编辑面板 {panel_info['message_id']} 时发生错误: {e}")


            except Exception as e:
                print(f"刷新统计面板时发生未知错误: {e}")

    @daily_stats_refresh.before_loop
    async def before_daily_stats_refresh(self):
        await self.bot.wait_until_ready()

    @tasks.loop(hours=3)
    async def bumping_task(self):
        print(f"[{datetime.now(TZ_SHANGHAI)}] 启动帖子顶帖任务...")

        forums_to_scan = []
        for guild in self.bot.guilds:
            for forum in guild.forums:
                if any(keyword in forum.name for keyword in RECOMMEND_TARGET_KEYWORDS):
                    forums_to_scan.append(forum)

        if not forums_to_scan: return

        recently_bumped_ids = await statistics_db.get_recently_bumped_threads(days=7)
        three_days_ago_utc = datetime.now(timezone.utc) - timedelta(days=3)

        for forum in forums_to_scan:
            eligible_threads = []
            try:
                for thread in forum.threads:
                    if thread.id in recently_bumped_ids or thread.archived:
                        continue

                    last_msg_time = None
                    try:
                        # 【关键修复】使用列表推导式代替 .flatten()
                        last_messages = [msg async for msg in thread.history(limit=1)]
                        if not last_messages: continue
                        last_message = last_messages[0]
                        last_msg_time = last_message.created_at
                    except (IndexError, discord.Forbidden):
                        continue

                    if last_msg_time and last_msg_time < three_days_ago_utc:
                        eligible_threads.append(thread)

            except discord.Forbidden:
                continue

            if not eligible_threads: continue

            num_to_bump = random.randint(5, 10)
            threads_to_bump = random.sample(eligible_threads, min(len(eligible_threads), num_to_bump))

            print(f"- 在频道 '{forum.name}' 中找到 {len(threads_to_bump)} 个帖子准备顶帖。")
            for thread in threads_to_bump:
                try:
                    msg = await thread.send(f"Bumping thread... ({random.randint(1000,9999)})")
                    await msg.delete()
                    await statistics_db.log_thread_bump(thread.id)
                    await asyncio.sleep(3)
                except Exception as e:
                    print(f"  - 顶帖失败: {thread.name} ({thread.id}) - {e}")

    @bumping_task.before_loop
    async def before_bumping_task(self):
        await self.bot.wait_until_ready()