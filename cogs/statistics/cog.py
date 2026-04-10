import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timedelta, timezone, time
import asyncio
import random

from . import db as statistics_db
from .views import ForumSelectView, StatisticsContainerView
from config import TZ_SHANGHAI, RECOMMEND_TARGET_KEYWORDS


TEST_MILESTONE_CHANNEL_ID = 1426616953975607476
LIKE_MILESTONE_1K = 1000
LIKE_MILESTONE_1K_KEY = "likes_1000"


class StatisticsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.add_view(StatisticsContainerView(stats_data={}, cog_instance=self))
        # 启动新的任务
        self.daily_stats_refresh.start()
        self.bumping_task.start()
        self.thread_cache_sync_task.start()

    async def cog_load(self):
        # 确保数据库表已创建
        await statistics_db.init_statistics_db()
        print("✅ [StatisticsCog] 已成功加载。")

    async def cog_unload(self):
        # 停止新的任务
        self.daily_stats_refresh.cancel()
        self.bumping_task.cancel()
        self.thread_cache_sync_task.cancel()

    def _serialize_dt(self, value: datetime | None) -> str | None:
        return value.isoformat() if value else None

    def _deserialize_dt(self, value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    def _build_milestone_embed(
        self, thread: discord.Thread, snapshot: dict
    ) -> discord.Embed:
        color = discord.Color.random()
        author_name = snapshot.get("author_name") or "未知作者"
        tags = snapshot.get("tags", [])
        tags_text = " / ".join(tags[:5]) if tags else "无标签"
        likes = snapshot.get("likes", 0)
        comments = snapshot.get("comments", 0)

        embed = discord.Embed(
            title="🎉 热门帖子突破 1K 赞",
            description=(
                f"**[{thread.name}]({thread.jump_url})** 获得了超过 **{LIKE_MILESTONE_1K}** 个赞，"
                f"快去围观这篇高热作品。"
            ),
            color=color,
            timestamp=datetime.now(TZ_SHANGHAI),
        )
        embed.add_field(name="作者", value=author_name, inline=True)
        embed.add_field(name="点赞 / 评论", value=f"{likes} / {comments}", inline=True)
        embed.add_field(
            name="所在论坛",
            value=thread.parent.mention if thread.parent else "未知论坛",
            inline=False,
        )
        embed.add_field(name="标签", value=tags_text, inline=False)
        embed.add_field(
            name="直达链接", value=f"[点击跳转到帖子]({thread.jump_url})", inline=False
        )
        embed.set_footer(text="统计系统自动播报")
        return embed

    async def maybe_send_like_milestone_notification(
        self, thread: discord.Thread, previous_likes: int | None, snapshot: dict
    ):
        current_likes = snapshot.get("likes", 0)
        crossed_threshold = current_likes >= LIKE_MILESTONE_1K and (
            previous_likes is None or previous_likes < LIKE_MILESTONE_1K
        )
        if not crossed_threshold:
            return

        if await statistics_db.has_thread_milestone_notification(
            thread.id, LIKE_MILESTONE_1K_KEY
        ):
            return

        target_channel = self.bot.get_channel(TEST_MILESTONE_CHANNEL_ID)
        if target_channel is None:
            try:
                target_channel = await self.bot.fetch_channel(TEST_MILESTONE_CHANNEL_ID)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                print(f"获取里程碑测试频道失败: {e}")
                return

        if not isinstance(target_channel, discord.abc.Messageable):
            print(f"里程碑测试频道不可发送消息: {TEST_MILESTONE_CHANNEL_ID}")
            return

        embed = self._build_milestone_embed(thread, snapshot)
        content = f"🥳 发现一篇帖子刚刚突破 **{LIKE_MILESTONE_1K}** 赞！"
        try:
            await target_channel.send(content=content, embed=embed)
            await statistics_db.record_thread_milestone_notification(
                thread.id, LIKE_MILESTONE_1K_KEY
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f"发送帖子里程碑通知失败 {thread.id}: {e}")

    async def _build_thread_snapshot(self, thread: discord.Thread) -> dict:
        starter = thread.starter_message
        if not starter:
            try:
                history = [
                    msg async for msg in thread.history(limit=1, oldest_first=True)
                ]
                starter = history[0] if history else None
            except (discord.errors.Forbidden, IndexError):
                starter = None

        last_message_at = None
        try:
            recent_messages = [msg async for msg in thread.history(limit=1)]
            if recent_messages:
                last_message_at = recent_messages[0].created_at
        except discord.errors.Forbidden:
            last_message_at = thread.created_at

        likes = sum(r.count for r in starter.reactions) if starter else 0
        comments = thread.message_count - 1 if thread.message_count > 0 else 0
        author = thread.owner

        return {
            "thread_id": thread.id,
            "guild_id": thread.guild.id,
            "forum_channel_id": thread.parent_id,
            "thread_name": thread.name,
            "thread_url": thread.jump_url,
            "author_id": author.id if author else None,
            "author_name": author.display_name if author else "未知作者",
            "created_at": self._serialize_dt(thread.created_at),
            "last_message_at": self._serialize_dt(last_message_at),
            "likes": likes,
            "comments": comments,
            "score": likes * 1.5 + comments,
            "tags": [tag.name for tag in thread.applied_tags],
            "is_archived": thread.archived,
            "last_synced_at": datetime.now(TZ_SHANGHAI).isoformat(),
        }

    async def sync_forum_thread_cache(self, forum: discord.ForumChannel):
        if not forum:
            return

        existing_threads = {
            item["thread_id"]: item
            for item in await statistics_db.get_cached_forum_threads(
                forum.id, include_archived=True
            )
        }
        active_thread_ids = []
        synced_at = datetime.now(TZ_SHANGHAI).isoformat()
        for thread in forum.threads:
            try:
                previous_snapshot = existing_threads.get(thread.id)
                previous_likes = (
                    previous_snapshot.get("likes")
                    if previous_snapshot is not None
                    else None
                )
                snapshot = await self._build_thread_snapshot(thread)
                snapshot["last_synced_at"] = synced_at
                await statistics_db.upsert_forum_thread_snapshot(snapshot)
                await self.maybe_send_like_milestone_notification(
                    thread, previous_likes, snapshot
                )
                active_thread_ids.append(thread.id)
                await asyncio.sleep(0.2)
            except discord.Forbidden:
                continue
            except Exception as e:
                print(f"同步帖子缓存失败 {thread.id}: {e}")

        await statistics_db.mark_missing_forum_threads_archived(
            forum.id, active_thread_ids, synced_at
        )

    async def ensure_forum_thread_cache(self, forum: discord.ForumChannel):
        summary = await statistics_db.get_cache_sync_summary(forum.id)
        last_synced_at = None
        total_count = 0
        if summary:
            last_synced_at = self._deserialize_dt(summary["last_synced_at"])
            total_count = summary["total_count"] or 0

        should_sync = total_count == 0
        if last_synced_at is not None:
            should_sync = should_sync or (
                datetime.now(TZ_SHANGHAI) - last_synced_at >= timedelta(minutes=30)
            )

        if should_sync:
            await self.sync_forum_thread_cache(forum)

    async def build_statistics_from_cache(self, forum: discord.ForumChannel):
        cached_threads = await statistics_db.get_cached_forum_threads(forum.id)
        if not cached_threads:
            return {
                "channel_name": forum.name,
                "channel_icon_url": forum.guild.icon.url if forum.guild.icon else None,
                "total_threads": 0,
                "new_threads_7d": 0,
                "hot_threads": [],
                "cold_threads": [],
            }

        seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)
        thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
        threads_with_stats = []

        for item in cached_threads:
            created_at = self._deserialize_dt(item.get("created_at"))
            threads_with_stats.append(
                {
                    "id": item["thread_id"],
                    "name": item["thread_name"],
                    "url": item.get("thread_url"),
                    "created_at": created_at,
                    "likes": item.get("likes", 0),
                    "comments": item.get("comments", 0),
                    "score": item.get("score", 0),
                    "author_name": item.get("author_name") or "未知作者",
                    "tags": item.get("tags", []),
                }
            )

        total_threads = len(threads_with_stats)
        new_threads_7d = sum(
            1
            for t in threads_with_stats
            if t["created_at"] and t["created_at"] > seven_days_ago
        )

        threads_with_stats.sort(key=lambda x: x["score"], reverse=True)
        hot_threads = threads_with_stats[:15]

        older_threads = [
            t
            for t in threads_with_stats
            if t["created_at"] and t["created_at"] < thirty_days_ago
        ]
        older_threads.sort(key=lambda x: x["score"])
        cold_threads = older_threads[:15]

        return {
            "channel_name": forum.name,
            "channel_icon_url": forum.guild.icon.url if forum.guild.icon else None,
            "total_threads": total_threads,
            "new_threads_7d": new_threads_7d,
            "hot_threads": hot_threads,
            "cold_threads": cold_threads,
        }

    async def gather_statistics(self, forum: discord.ForumChannel):
        """
        收集单个论坛的统计数据。
        """
        if not forum:
            return None
        await self.ensure_forum_thread_cache(forum)
        return await self.build_statistics_from_cache(forum)

    # ==========================================
    # Part 1. 斜杠命令
    # ==========================================
    statistics_group = app_commands.Group(name="统计", description="统计面板相关命令")

    @statistics_group.command(
        name="发送选择器", description="[管理] 发送一个用于创建统计面板的频道选择器"
    )
    @app_commands.default_permissions(manage_guild=True)
    async def send_selector(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "请在下方选择要为其生成统计面板的论坛频道：",
            view=ForumSelectView(self),
            ephemeral=True,
        )

    @statistics_group.command(
        name="清理无效面板",
        description="[管理] 清理数据库中已在Discord被删除的面板记录",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def cleanup_panels(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        all_panels = await statistics_db.get_all_statistics_panels()
        if not all_panels:
            return await interaction.followup.send(
                "数据库中没有面板记录，无需清理。", ephemeral=True
            )

        cleaned_count = 0
        for panel_data in all_panels:
            try:
                channel = self.bot.get_channel(panel_data["panel_channel_id"])
                if channel:
                    await channel.fetch_message(panel_data["message_id"])
                else:
                    raise discord.NotFound(None, "Channel not found")
            except discord.NotFound:
                await statistics_db.remove_statistics_panel(panel_data["message_id"])
                cleaned_count += 1

        await interaction.followup.send(
            f"✅ 清理完成！共移除了 **{cleaned_count}** 条无效的面板记录。",
            ephemeral=True,
        )

    # ==========================================
    # Part 2. 后台任务
    # ==========================================
    @tasks.loop(time=time(hour=0, minute=0, tzinfo=TZ_SHANGHAI))
    async def daily_stats_refresh(self):
        print(f"[{datetime.now(TZ_SHANGHAI)}] 启动每日统计刷新任务...")
        panels = await statistics_db.get_all_statistics_panels()

        for panel_info in panels:
            try:
                guild = self.bot.get_guild(panel_info["guild_id"])
                if not guild:
                    continue

                panel_channel = guild.get_channel(panel_info["panel_channel_id"])
                forum_channel = guild.get_channel(panel_info["forum_channel_id"])

                if (
                    not panel_channel
                    or not forum_channel
                    or not isinstance(forum_channel, discord.ForumChannel)
                ):
                    continue

                stats_data = await self.gather_statistics(forum_channel)
                if not stats_data:
                    continue

                try:
                    msg = await panel_channel.fetch_message(panel_info["message_id"])
                    new_view = StatisticsContainerView(
                        stats_data=stats_data,
                        cog_instance=self,
                        panel_message_id=msg.id,
                    )
                    await msg.edit(view=new_view)
                    print(f"- [成功] 已刷新版面 {msg.id} (关于 {forum_channel.name})")
                except discord.NotFound:
                    await statistics_db.remove_statistics_panel(
                        panel_info["message_id"]
                    )
                    print(
                        f"- [警告] 找不到统计面板消息 {panel_info['message_id']}，已从数据库移除。"
                    )
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

        if not forums_to_scan:
            return

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
                        if not last_messages:
                            continue
                        last_message = last_messages[0]
                        last_msg_time = last_message.created_at
                    except (IndexError, discord.Forbidden):
                        continue

                    if last_msg_time and last_msg_time < three_days_ago_utc:
                        eligible_threads.append(thread)

            except discord.Forbidden:
                continue

            if not eligible_threads:
                continue

            num_to_bump = random.randint(5, 10)
            threads_to_bump = random.sample(
                eligible_threads, min(len(eligible_threads), num_to_bump)
            )

            print(
                f"- 在频道 '{forum.name}' 中找到 {len(threads_to_bump)} 个帖子准备顶帖。"
            )
            for thread in threads_to_bump:
                try:
                    msg = await thread.send(
                        f"Bumping thread... ({random.randint(1000, 9999)})"
                    )
                    await msg.delete()
                    await statistics_db.log_thread_bump(thread.id)
                    await asyncio.sleep(3)
                except Exception as e:
                    print(f"  - 顶帖失败: {thread.name} ({thread.id}) - {e}")

    @bumping_task.before_loop
    async def before_bumping_task(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=30)
    async def thread_cache_sync_task(self):
        print(f"[{datetime.now(TZ_SHANGHAI)}] 启动论坛帖子缓存同步任务...")

        forums_to_sync = {}
        panels = await statistics_db.get_all_statistics_panels()
        for panel_info in panels:
            guild = self.bot.get_guild(panel_info["guild_id"])
            if not guild:
                continue
            forum = guild.get_channel(panel_info["forum_channel_id"])
            if isinstance(forum, discord.ForumChannel):
                forums_to_sync[forum.id] = forum

        for guild in self.bot.guilds:
            for forum in guild.forums:
                if any(keyword in forum.name for keyword in RECOMMEND_TARGET_KEYWORDS):
                    forums_to_sync[forum.id] = forum

        for forum in forums_to_sync.values():
            try:
                await self.sync_forum_thread_cache(forum)
            except Exception as e:
                print(f"同步论坛缓存失败 {forum.id}: {e}")

    @thread_cache_sync_task.before_loop
    async def before_thread_cache_sync_task(self):
        await self.bot.wait_until_ready()
