import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timedelta, timezone, time
import asyncio
import random

from . import db as statistics_db
from .views import ForumSelectView, StatisticsContainerView
from config import (
    TZ_SHANGHAI,
    RECOMMEND_TARGET_KEYWORDS,
    MILESTONE_FIRST_BREAK_CHANNEL_ID,
)


LIKE_MILESTONE_STEP = 1000
FIRST_BREAK_MILESTONES = (1000, 10000)
MILESTONE_CONTENT_LINES = [
    "奇米蛋闻到香香高热帖的味道啦，这颗作品蛋已经啵的一下冲破 **{milestone}** 赞！",
    "叮咚——检测到超级闪亮的内容蛋，这篇帖子已经被大家亲亲到 **{milestone}** 赞啦！",
    "奇米蛋滚过来报喜，这篇宝藏产出已经孵成金灿灿的 **{milestone}** 赞大蛋啦！",
    "呀呼，这篇帖子被喜欢到冒小星星，已经顺利蹦到 **{milestone}** 赞咯！",
    "报告报告，论坛里有一颗超人气奇米蛋作品，刚刚甜甜地突破 **{milestone}** 赞！",
]
MILESTONE_EMBED_DESCRIPTIONS = [
    "这是一篇被大家狠狠干饭式点赞的作品，奇米蛋已经抱着小喇叭来庆祝啦。",
    "喜欢值已经满到溢出来啦，快点进帖子围观这颗闪闪发光的内容蛋。",
    "能量检测结果：超高热度、超多喜欢、超值得跳进去看看。",
    "这篇帖子已经成功进化成 {milestone} 赞甜心明星帖，路过的都可以去蹭蹭好运。",
    "一颗作品蛋被大家宠到发光，现在正处于“怎么这么香呀”状态。",
]


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

    async def _get_thread_starter(
        self, thread: discord.Thread
    ) -> discord.Message | None:
        starter = thread.starter_message
        if starter:
            return starter

        try:
            history = [msg async for msg in thread.history(limit=1, oldest_first=True)]
            return history[0] if history else None
        except (discord.errors.Forbidden, IndexError):
            return None

    def _get_message_image_url(self, message: discord.Message | None) -> str | None:
        if not message:
            return None

        for attachment in message.attachments:
            content_type = attachment.content_type or ""
            if content_type.startswith("image/"):
                return attachment.url

        for embed in message.embeds:
            if embed.image and embed.image.url:
                return embed.image.url
            if embed.thumbnail and embed.thumbnail.url:
                return embed.thumbnail.url

        return None

    def _build_milestone_embed(self, snapshot: dict, milestone: int) -> discord.Embed:
        color = discord.Color.random()
        author_name = snapshot.get("author_name") or "未知作者"
        tags = snapshot.get("tags", [])
        tags_text = " / ".join(tags[:5]) if tags else "无标签"
        likes = snapshot.get("likes", 0)
        comments = snapshot.get("comments", 0)
        description = random.choice(MILESTONE_EMBED_DESCRIPTIONS).format(
            milestone=f"{milestone:,}"
        )
        forum_channel = self.bot.get_channel(snapshot["forum_channel_id"])
        forum_text = (
            forum_channel.mention
            if forum_channel
            else f"`{snapshot['forum_channel_id']}`"
        )
        thread_url = snapshot.get("thread_url") or "https://discord.com/channels/@me"
        thread_name = snapshot.get("thread_name") or "未知帖子"
        starter_image_url = snapshot.get("starter_image_url")

        embed = discord.Embed(
            title=f"🎉 热门帖子突破 {milestone:,} 赞",
            description=f"**[{thread_name}]({thread_url})** 获得了超过 **{milestone:,}** 个赞。\n{description}",
            color=color,
            timestamp=datetime.now(TZ_SHANGHAI),
        )
        embed.add_field(name="作者", value=author_name, inline=True)
        embed.add_field(name="点赞 / 评论", value=f"{likes} / {comments}", inline=True)
        embed.add_field(name="所在论坛", value=forum_text, inline=False)
        embed.add_field(name="标签", value=tags_text, inline=False)
        embed.add_field(
            name="直达链接", value=f"[点击跳转到帖子]({thread_url})", inline=False
        )
        if starter_image_url:
            embed.set_thumbnail(url=starter_image_url)
        embed.set_footer(text="统计系统自动播报")
        return embed

    async def _fetch_messageable_channel(self, channel_id: int):
        target_channel = self.bot.get_channel(channel_id)
        if target_channel is None:
            try:
                target_channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                print(f"获取频道失败 {channel_id}: {e}")
                return None

        if not isinstance(target_channel, discord.abc.Messageable):
            print(f"频道不可发送消息: {channel_id}")
            return None

        return target_channel

    async def _fetch_thread_channel(self, thread_id: int):
        thread = self.bot.get_channel(thread_id)
        if thread is None:
            try:
                thread = await self.bot.fetch_channel(thread_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                print(f"获取原帖频道失败 {thread_id}: {e}")
                return None

        if not isinstance(thread, discord.Thread):
            return None
        if thread.archived:
            return None
        return thread

    async def send_like_milestone_notification(self, snapshot: dict, milestone: int):
        thread_channel = await self._fetch_thread_channel(snapshot["thread_id"])
        if not thread_channel:
            return False

        embed = self._build_milestone_embed(snapshot, milestone)
        content = "🥳 " + random.choice(MILESTONE_CONTENT_LINES).format(
            milestone=f"{milestone:,}"
        )
        try:
            await thread_channel.send(content=content, embed=embed)
            await statistics_db.set_thread_like_milestone(snapshot["thread_id"], milestone)
            return True
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f"发送帖子里程碑通知失败 {snapshot['thread_id']}: {e}")
            return False

    async def send_first_break_notification(self, snapshot: dict, milestone: int):
        if milestone not in FIRST_BREAK_MILESTONES:
            return False

        target_channel = await self._fetch_messageable_channel(
            MILESTONE_FIRST_BREAK_CHANNEL_ID
        )
        if target_channel is None:
            return False

        thread_name = snapshot.get("thread_name") or "未知帖子"
        thread_url = snapshot.get("thread_url") or "https://discord.com/channels/@me"
        likes = snapshot.get("likes", 0)
        content = (
            f"📈 帖子 **{thread_name}** 首次突破 **{milestone:,}** 赞！\n"
            f"当前点赞：**{likes:,}**\n"
            f"原帖直达：{thread_url}"
        )
        embed = self._build_milestone_embed(snapshot, milestone)

        try:
            await target_channel.send(content=content, embed=embed)
            await statistics_db.mark_first_break_milestone_notified(
                snapshot["thread_id"], milestone
            )
            return True
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f"发送首破里程碑通知失败 {snapshot['thread_id']} ({milestone}): {e}")
            return False

    async def _build_thread_snapshot(self, thread: discord.Thread) -> dict:
        starter = await self._get_thread_starter(thread)

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
            "starter_image_url": self._get_message_image_url(starter),
            "is_archived": thread.archived,
            "last_synced_at": datetime.now(TZ_SHANGHAI).isoformat(),
        }

    async def sync_forum_thread_cache(self, forum: discord.ForumChannel):
        if not forum:
            return

        active_thread_ids = []
        synced_at = datetime.now(TZ_SHANGHAI).isoformat()
        for thread in forum.threads:
            try:
                snapshot = await self._build_thread_snapshot(thread)
                snapshot["last_synced_at"] = synced_at
                await statistics_db.upsert_forum_thread_snapshot(snapshot)
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
                "forum_channel_id": forum.id,
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
            "forum_channel_id": forum.id,
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

    async def process_like_milestones(self):
        milestone_candidates = (
            await statistics_db.get_threads_ready_for_like_milestones(
                LIKE_MILESTONE_STEP
            )
        )

        processed_count = 0
        regular_sent_count = 0
        first_break_sent_count = 0

        for snapshot in milestone_candidates:
            processed_count += 1
            likes = snapshot.get("likes", 0)
            target_milestone = (likes // LIKE_MILESTONE_STEP) * LIKE_MILESTONE_STEP
            last_milestone = snapshot.get("last_like_milestone", 0)
            if (
                target_milestone < LIKE_MILESTONE_STEP
                or target_milestone <= last_milestone
            ):
                target_milestone = None

            if target_milestone is not None:
                sent_regular = await self.send_like_milestone_notification(
                    snapshot, target_milestone
                )
                if sent_regular:
                    regular_sent_count += 1

            notified_first_1000 = snapshot.get("notified_first_1000", 0)
            notified_first_10000 = snapshot.get("notified_first_10000", 0)
            if likes >= 1000 and not notified_first_1000:
                sent = await self.send_first_break_notification(snapshot, 1000)
                if sent:
                    first_break_sent_count += 1
            if likes >= 10000 and not notified_first_10000:
                sent = await self.send_first_break_notification(snapshot, 10000)
                if sent:
                    first_break_sent_count += 1

            await asyncio.sleep(0.5)

        return {
            "processed_count": processed_count,
            "regular_sent_count": regular_sent_count,
            "first_break_sent_count": first_break_sent_count,
        }

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

    @statistics_group.command(
        name="初始化里程碑通知",
        description="[管理] 重置并重发当前所有里程碑通知（按当前最高里程碑）",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def reinitialize_milestone_notifications(
        self, interaction: discord.Interaction
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)

        await statistics_db.reset_all_thread_milestone_state()
        result = await self.process_like_milestones()

        await interaction.followup.send(
            (
                "✅ 里程碑通知初始化完成。\n"
                f"扫描帖子：**{result['processed_count']}**\n"
                f"原帖里程碑通知：**{result['regular_sent_count']}**\n"
                f"首破 1000/10000 专用通知：**{result['first_break_sent_count']}**"
            ),
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
                        forum_channel_id=forum_channel.id,
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
        for guild in self.bot.guilds:
            for forum in guild.forums:
                forums_to_sync[forum.id] = forum

        for forum in forums_to_sync.values():
            try:
                await self.sync_forum_thread_cache(forum)
            except Exception as e:
                print(f"同步论坛缓存失败 {forum.id}: {e}")

        result = await self.process_like_milestones()
        print(
            "里程碑通知任务完成: "
            f"扫描 {result['processed_count']}，"
            f"原帖通知 {result['regular_sent_count']}，"
            f"首破通知 {result['first_break_sent_count']}"
        )

    @thread_cache_sync_task.before_loop
    async def before_thread_cache_sync_task(self):
        await self.bot.wait_until_ready()
