import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timedelta, time
import asyncio
import random

from config import TZ_SHANGHAI, RECOMMEND_TARGET_KEYWORDS
from . import db as statistics_db
from .views import StatisticsContainerView

class StatisticsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # 在Bot启动时，用一个临时的、结构有效的数据添加视图，以便 custom_id 被注册
        # 实际显示的数据将在发送时或刷新时生成
        placeholder_data = {
            'channel_name': '加载中', 'total_threads': 0, 'new_threads_7d': 0,
            'hot_threads': [], 'cold_threads': []
        }
        self.bot.add_view(StatisticsContainerView(stats_data=placeholder_data))

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
        # 评论数 = 消息总数 - 1 (起始帖)
        comments = thread.message_count - 1 if thread.message_count and thread.message_count > 0 else 0
        try:
            # 尝试获取起始消息以统计点赞
            starter_message = thread.starter_message
            if not starter_message:
                starter_message = await thread.fetch_message(thread.id)

            if starter_message and starter_message.reactions:
                likes = sum(r.count for r in starter_message.reactions)
        except (discord.NotFound, discord.Forbidden):
            # 帖子可能被删或Bot无权限
            pass

        # 热度计分：评论的权重更高
        score = likes + (comments * 2)
        return likes, comments, score

    async def gather_statistics(self, forum: discord.ForumChannel) -> dict:
        """为指定的论坛频道收集和处理统计数据"""
        now = datetime.now(TZ_SHANGHAI)
        seven_days_ago = now - timedelta(days=7)

        all_threads = []
        try:
            # 同时获取活跃和已归档的帖子以获得真实数量
            all_threads.extend(forum.threads)
            async for thread in forum.archived_threads(limit=None):
                all_threads.append(thread)
        except discord.Forbidden:
             return None # 无法访问此频道

        new_threads_7d = 0

        sem = asyncio.Semaphore(10) # 限制并发以避免API速率限制
        tasks = []

        async def process_thread(thread):
            async with sem:
                # 检查创建时间
                creation_time_aware = thread.created_at.astimezone(TZ_SHANGHAI)
                is_new = creation_time_aware > seven_days_ago

                likes, comments, score = await self._get_thread_interaction(thread)
                return {
                    'is_new': is_new,
                    'name': thread.name,
                    'url': thread.jump_url,
                    'likes': likes,
                    'comments': comments,
                    'score': score,
                    'created_at': creation_time_aware
                }

        thread_details = await asyncio.gather(*(process_thread(t) for t in all_threads if t))

        new_threads_7d = sum(1 for t in thread_details if t['is_new'])

        # 排序以获取热门和冷门帖子
        thread_details.sort(key=lambda x: x['score'], reverse=True)
        hot_threads = thread_details[:5]

        # 定义冷门宝藏: 创建超过3天，有互动(分数>0)，但分数较低
        three_days_ago = now - timedelta(days=3)
        potential_cold = [
            t for t in thread_details
            if t['created_at'] < three_days_ago and t['score'] > 0
        ]
        potential_cold.sort(key=lambda x: x['score']) # 按分数升序
        cold_threads = potential_cold[:5]

        return {
            'channel_name': forum.name,
            'channel_icon_url': forum.guild.icon.url if forum.guild.icon else None,
            'total_threads': len(all_threads),
            'new_threads_7d': new_threads_7d,
            'hot_threads': hot_threads,
            'cold_threads': cold_threads
        }

    # ==========================================
    # Part 2. 斜杠命令
    # ==========================================
    @app_commands.command(name="创建统计面板", description="[管理] 为指定频道创建或更新数据统计面板")
    @app_commands.describe(channels="选择1-5个论坛频道进行统计")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def create_stats_panel(self, interaction: discord.Interaction, channels: app_commands.Range[discord.ForumChannel, 1, 5]):
        await interaction.response.send_message(f"收到指令！正在为 {len(channels)} 个频道生成统计数据，这可能需要一点时间...", ephemeral=True)

        for channel in channels:
            stats_data = await self.gather_statistics(channel)
            if stats_data is None:
                await interaction.followup.send(f"❌ 无法访问频道 {channel.mention} 或读取其内容。", ephemeral=True)
                continue

            view = StatisticsContainerView(stats_data=stats_data)
            # 将面板发送在命令执行的频道
            msg = await interaction.channel.send(view=view)

            # 记录到数据库以便每日刷新
            await statistics_db.add_statistics_panel(msg.id, msg.channel.id, channel.id, interaction.guild.id)

        await interaction.followup.send("✅ 所有统计面板均已创建！它们将每日自动刷新。", ephemeral=True)

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
                    # 如果消息被删除，可以考虑从数据库移除记录
                    print(f"- [警告] 找不到统计面板消息 {panel_info['message_id']}，可能已被删除。")
                except discord.Forbidden:
                    print(f"- [错误] 没有权限编辑消息 {panel_info['message_id']}。")

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

        # 获取7天内顶过的帖子，避免重复
        recently_bumped_ids = await statistics_db.get_recently_bumped_threads(days=7)
        three_days_ago_utc = datetime.utcnow() - timedelta(days=3)

        for forum in forums_to_scan:
            eligible_threads = []
            try:
                # 只处理未归档的帖子
                for thread in forum.threads:
                    if thread.id in recently_bumped_ids or thread.archived:
                        continue

                    last_msg_time = None
                    try:
                        # history比last_message更可靠
                        last_message = (await thread.history(limit=1).flatten())[0]
                        last_msg_time = last_message.created_at # UTC aware
                    except (IndexError, discord.Forbidden):
                        continue # 没有消息或权限

                    if last_msg_time.replace(tzinfo=None) < three_days_ago_utc:
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
                    await asyncio.sleep(3) # API友好
                except Exception as e:
                    print(f"  - 顶帖失败: {thread.name} ({thread.id}) - {e}")

    @bumping_task.before_loop
    async def before_bumping_task(self):
        await self.bot.wait_until_ready()