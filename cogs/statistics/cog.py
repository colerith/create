import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timedelta, time
import asyncio
import random

from config import TZ_SHANGHAI, RECOMMEND_TARGET_KEYWORDS
from . import db as statistics_db
from .views import StatisticsContainerView, ForumSelectView

class StatisticsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.add_view(StatisticsContainerView(stats_data={
            'channel_name': '加载中', 'total_threads': 0, 'new_threads_7d': 0,
            'hot_threads': [], 'cold_threads': []
        }))


    async def cog_load(self):
        """Cog加载时初始化数据库并启动后台任务"""
        await statistics_db.init_statistics_db()
        self.daily_stats_refresh.start()
        self.bumping_task.start()
        print("✅ [StatisticsCog] 数据库已初始化，后台任务已启动。")

    async def cog_unload(self):
        """Cog卸载时停止后台任务"""
        self.daily_stats_refresh.cancel()
        self.bumping_task.cancel()

    # ==========================================
    # Part 1. 核心统计逻辑
    # ==========================================
    async def _get_thread_interaction(self, thread: discord.Thread):
        """辅助函数：获取单个帖子的点赞、评论和热度分"""
        likes = 0
        comments = thread.message_count - 1 if thread.message_count and thread.message_count > 0 else 0
        try:
            starter_message = thread.starter_message
            if not starter_message:
                history = thread.history(limit=1, oldest_first=True)
                starter_message = await history.__anext__()

            if starter_message and starter_message.reactions:
                likes = sum(r.count for r in starter_message.reactions)
        except (StopAsyncIteration, discord.NotFound, discord.Forbidden):
            pass

        score = likes + (comments * 2)
        return likes, comments, score

    async def gather_statistics(self, forum: discord.ForumChannel):
        """
        [已更新] 收集单个论坛的统计数据, 包括作者和标签。
        """
        if not forum:
            return None

        threads = forum.threads
        if not threads:
            return {
                "channel_name": forum.name,
                "channel_icon_url": forum.guild.icon.url if forum.guild.icon else None,
                "total_threads": 0, "new_threads_7d": 0,
                "hot_threads": [], "cold_threads": []
            }

        seven_days_ago = datetime.utcnow() - timedelta(days=7)
        total_threads = len(threads)
        new_threads_7d = sum(1 for t in threads if t.created_at > seven_days_ago)

        threads_with_stats = []
        for t in threads:
            starter = t.starter_message
            if not starter:
                try: # 兜底获取
                    history = await t.history(limit=1, oldest_first=True).flatten()
                    starter = history[0] if history else None
                except (discord.errors.Forbidden, IndexError):
                    starter = None

            likes = sum(r.count for r in starter.reactions) if starter else 0
            comments = t.message_count -1 if t.message_count > 0 else 0

            # 【新增】获取作者和标签
            author_name = t.owner.display_name if t.owner else "未知作者"
            tags = [tag.name for tag in t.applied_tags]

            threads_with_stats.append({
                "id": t.id,
                "name": t.name,
                "url": t.jump_url,
                "created_at": t.created_at,
                "likes": likes,
                "comments": comments,
                "score": likes * 1.5 + comments,
                "author_name": author_name, # <-- 新增
                "tags": tags # <-- 新增
            })

        # 排序
        threads_with_stats.sort(key=lambda x: x['score'], reverse=True)
        hot_threads = threads_with_stats[:15]

        now = datetime.utcnow()
        thirty_days_ago = now - timedelta(days=30)
        older_threads = [t for t in threads_with_stats if t['created_at'] < thirty_days_ago]
        older_threads.sort(key=lambda x: x['score'], reverse=False) # 按分数从低到高
        cold_threads = older_threads[:15]


        return {
            "channel_name": forum.name,
            "channel_icon_url": forum.guild.icon.url if forum.guild.icon else None,
            "total_threads": total_threads,
            "new_threads_7d": new_threads_7d,
            "hot_threads": hot_threads,
            "cold_threads": cold_threads
        }


    # ==========================================
    # Part 2. 斜杠命令
    # ==========================================
    @app_commands.command(name="创建统计面板", description="[管理] 为指定的论坛频道创建一份数据统计报告")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def create_stats_panel(self, interaction: discord.Interaction):
        # 每次调用命令时，都创建一个新的、临时的 ForumSelectView 实例
        view = ForumSelectView(self)
        await interaction.response.send_message(
            "请在下方选择你想要生成统计报告的论坛频道 (可多选)，然后点击按钮确认。",
            view=view,
            ephemeral=True
        )

    # ==========================================
    # Part 3. 后台任务
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

                if not panel_channel or not forum_channel or not isinstance(forum_channel, discord.ForumChannel): continue

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
                except Exception as e:
                    print(f"- [错误] 编辑消息时出错 {panel_info['message_id']}: {e}")

            except Exception as e:
                print(f"刷新统计面板时发生未知错误: {e}")

    @daily_stats_refresh.before_loop
    async def before_daily_stats_refresh(self):
        await self.bot.wait_until_ready()

    @tasks.loop(hours=3)
    async def bumping_task(self):
        print(f"[{datetime.now(TZ_SHANGHAI)}] 启动帖子顶帖任务...")
        forums_to_scan = [f for g in self.bot.guilds for f in g.forums if any(k in f.name for k in RECOMMEND_TARGET_KEYWORDS)]
        if not forums_to_scan: return

        recently_bumped_ids = await statistics_db.get_recently_bumped_threads(days=7)
        three_days_ago_utc = datetime.utcnow() - timedelta(days=3)

        for forum in forums_to_scan:
            eligible_threads = []
            try:
                for thread in forum.threads:
                    if thread.id in recently_bumped_ids or thread.archived: continue
                    try:
                        last_message = (await thread.history(limit=1).flatten())[0]
                        if last_message.created_at.replace(tzinfo=None) < three_days_ago_utc:
                            eligible_threads.append(thread)
                    except (IndexError, discord.Forbidden): continue
            except discord.Forbidden: continue

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
