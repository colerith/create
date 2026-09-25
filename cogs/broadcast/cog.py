import asyncio
import logging
import random
from datetime import datetime, time, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import (
    EXPLORATION_TARGET_CHANNEL_IDS,
    HOURLY_BROADCAST_CHANNEL_ID,
    HOURLY_BROADCAST_ENABLED,
    HOURLY_BROADCAST_GUILD_ID,
    TZ_SHANGHAI,
)
from ..core.db import get_panel_message_id
from . import db
from .recommendations import recommendation_candidates, thread_details, work_category
from .render import RECOMMENDATION_FIELD, TITLE, build_embed, completed_hour, recent_threads


# 复用 bot.run 默认配置的 discord 日志处理器，让 PM2 能看到发送/跳过记录。
log = logging.getLogger("discord.hourly_broadcast")


class BroadcastCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._send_lock = asyncio.Lock()

    async def cog_load(self):
        await db.init_db()
        if HOURLY_BROADCAST_ENABLED:
            self.hourly_broadcast.start()

    async def cog_unload(self):
        self.hourly_broadcast.cancel()

    @staticmethod
    def is_source_channel(channel):
        if channel is None or channel.guild.id != HOURLY_BROADCAST_GUILD_ID:
            return False
        if isinstance(channel, discord.Thread) and channel.is_private():
            return False
        subject = channel.parent if isinstance(channel, discord.Thread) else channel
        if subject is None or subject.guild.me is None:
            return False
        # 与“我的作品”相同，使用指定服务器中机器人可查看的分区。
        return subject.permissions_for(subject.guild.me).view_channel

    @commands.Cog.listener()
    async def on_thread_create(self, thread):
        if (HOURLY_BROADCAST_ENABLED and isinstance(thread.parent, discord.ForumChannel)
                and self.is_source_channel(thread) and thread.created_at):
            await db.record_thread(thread)

    async def collect(self, guild, start, end):
        forums = {forum.id: forum for forum in guild.forums if self.is_source_channel(forum)}
        cached, events, update_rows = await db.get_inputs(guild.id, start, end)
        cached = [row for row in cached if row["forum_channel_id"] in forums]
        known_threads = {row["thread_id"] for row in [*cached, *events]
                         if row["forum_channel_id"] in forums}
        live_threads = [thread for forum in forums.values() for thread in forum.threads]
        threads = recent_threads(cached, events, live_threads, forums, start, end)

        # 一小时内同一作品多次发更新，只列最新的一次，点击可进入最新更新消息。
        updates = {}
        checked_channels = {}
        for row in update_rows:
            channel_id = row["channel_id"]
            if channel_id in updates:
                continue
            if channel_id not in checked_channels:
                channel = guild.get_channel_or_thread(channel_id)
                if channel is None and channel_id not in known_threads:
                    try:
                        channel = await self.bot.fetch_channel(channel_id)
                    except (discord.NotFound, discord.Forbidden):
                        checked_channels[channel_id] = False
                        continue
                checked_channels[channel_id] = (self.is_source_channel(channel) if channel
                                                else channel_id in known_threads)
            if checked_channels[channel_id]:
                updates[channel_id] = row
        return threads, list(updates.values()), cached, forums

    async def daily_report_url(self, guild):
        for channel_id in EXPLORATION_TARGET_CHANNEL_IDS:
            if not self.is_source_channel(guild.get_channel(channel_id)):
                continue
            message_id = await get_panel_message_id(channel_id, "daily_report")
            if message_id:
                return f"https://discord.com/channels/{guild.id}/{channel_id}/{message_id}"
        return None

    async def choose_recommendations(self, guild, cached, forums, count):
        candidates = recommendation_candidates(guild, cached, forums)
        selected = []
        # 包含归档缓存，抽中后验证帖子仍存在、可访问且未变成置顶/小剧场。
        # 限制失效候选重试数量，避免大量历史删帖导致一小时任务请求失控。
        for row in random.sample(candidates, min(len(candidates), 50)):
            thread = guild.get_thread(row["thread_id"])
            if thread is None:
                try:
                    thread = await self.bot.fetch_channel(row["thread_id"])
                except (discord.NotFound, discord.Forbidden):
                    continue
            if (not isinstance(thread, discord.Thread) or not self.is_source_channel(thread)
                    or thread.parent_id not in forums):
                continue
            forum = forums[thread.parent_id]
            fresh = thread_details(thread, forum, row)
            category = work_category(forum.name, fresh["thread_name"], fresh["tags"])
            if category is None or fresh["is_pinned"]:
                continue
            fresh.update(category=category, forum_name=forum.name)
            selected.append(fresh)
            if len(selected) == count:
                break
        return selected

    async def prepare_embed(self, guild, start, end):
        threads, updates, cached, forums = await self.collect(guild, start, end)
        # 以推送时段的结束日期计每日额度；午夜的简报属于新一天。
        date = end.date().isoformat()
        daily_due = not await db.recommendation_sent(guild.id, HOURLY_BROADCAST_CHANNEL_ID, date)
        fallback = not threads and not updates
        recommendations = await self.choose_recommendations(
            guild, cached, forums, 5 if fallback else 1,
        ) if fallback or daily_due else []
        if fallback and not recommendations:
            return None, False
        embed = build_embed(
            guild, start, end, threads, updates, cached, len(forums),
            recommendation=recommendations[0] if recommendations and not fallback else None,
            random_recommendations=recommendations if fallback else None,
            daily_recommendation=daily_due,
            daily_url=await self.daily_report_url(guild),
        )
        return embed, daily_due and bool(recommendations)

    async def find_sent_message(self, channel, end):
        # 处理“Discord 已发送成功，但写数据库前进程退出/连接超时”的情况。
        # 使用机器人作者、固定标题和小时结束时间共同识别，避免重试重复推送。
        date = end.date().isoformat()
        already_recommended = await db.recommendation_sent(channel.guild.id, channel.id, date)
        scan_start = end if already_recommended else end.replace(hour=0)
        # 补发可能发生在数小时后，不能只查原时段后一小时内创建的消息。
        async for message in channel.history(after=scan_start, limit=None):
            if message.author.id != self.bot.user.id:
                continue
            for embed in message.embeds:
                if embed.title != TITLE or embed.timestamp is None:
                    continue
                sent_end = embed.timestamp.astimezone(TZ_SHANGHAI)
                has_recommendation = any(field.name == RECOMMENDATION_FIELD for field in embed.fields)
                if sent_end == end:
                    return message.id, has_recommendation
                if (has_recommendation and not already_recommended
                        and sent_end.date() == end.date()
                        and sent_end.minute == sent_end.second == sent_end.microsecond == 0):
                    # 若发送后立即重启，下次整点先恢复前一个时段的日推额度。
                    await db.mark_processed(channel.guild.id, channel.id, sent_end, message.id, date)
                    already_recommended = True
        return None

    async def send_window(self, start, end, *, makeup=False):
        async with self._send_lock:
            if makeup:
                record = await db.get_record(HOURLY_BROADCAST_GUILD_ID, HOURLY_BROADCAST_CHANNEL_ID, end)
                if record and record["message_id"]:
                    return "already_sent", record["message_id"]
            elif await db.is_processed(HOURLY_BROADCAST_GUILD_ID, HOURLY_BROADCAST_CHANNEL_ID, end):
                return "processed", None
            guild = self.bot.get_guild(HOURLY_BROADCAST_GUILD_ID)
            if guild is None:
                raise RuntimeError("小时简报：机器人尚未加入指定服务器，或服务器缓存尚未就绪")
            channel = self.bot.get_channel(HOURLY_BROADCAST_CHANNEL_ID)
            if channel is None:
                channel = await self.bot.fetch_channel(HOURLY_BROADCAST_CHANNEL_ID)
            if not isinstance(channel, discord.TextChannel) or channel.guild.id != guild.id:
                raise RuntimeError("小时简报：目标必须是指定服务器内的文字频道")
            permissions = channel.permissions_for(guild.me)
            if not all((permissions.view_channel, permissions.send_messages,
                        permissions.embed_links, permissions.read_message_history)):
                raise RuntimeError("小时简报需要目标频道的查看频道、发送消息、嵌入链接、读取消息历史权限")

            recovered = await self.find_sent_message(channel, end)
            if recovered:
                message_id, has_recommendation = recovered
            else:
                embed, has_recommendation = await self.prepare_embed(guild, start, end)
                if embed is None:
                    await db.mark_processed(guild.id, channel.id, end)
                    log.info("[小时简报] %s 无新帖、更新或可用推荐候选，跳过", end.isoformat())
                    return "empty", None
                message = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                message_id = message.id
            await db.mark_processed(
                guild.id, channel.id, end, message_id,
                end.date().isoformat() if has_recommendation else None,
            )
            log.info("[小时简报] %s 已推送至 %s，消息 %s", end.isoformat(), channel.id, message_id)
            return "recovered" if recovered else "sent", message_id

    @tasks.loop(time=[time(hour=hour, tzinfo=TZ_SHANGHAI) for hour in range(24)])
    async def hourly_broadcast(self):
        start, end = completed_hour(datetime.now(TZ_SHANGHAI))
        scheduler = getattr(self.bot, "discord_request_scheduler", None)
        if scheduler:
            scheduler.set_current_priority(20)
        for attempt in range(3):
            try:
                await self.send_window(start, end)
                return
            except Exception:
                log.exception("[小时简报] 推送失败（%s/3）", attempt + 1)
                if attempt < 2:
                    await asyncio.sleep(30)

    @hourly_broadcast.before_loop
    async def before_hourly_broadcast(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="预览小时简报", description="[管理] 私下预览上一完整小时的简报，不发布到频道")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def preview(self, interaction: discord.Interaction):
        if (interaction.guild_id != HOURLY_BROADCAST_GUILD_ID
                or not interaction.user.guild_permissions.administrator):
            await interaction.response.send_message("仅限目标服务器的管理员使用。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        start, end = completed_hour(datetime.now(TZ_SHANGHAI))
        embed, _ = await self.prepare_embed(interaction.guild, start, end)
        if embed is None:
            await interaction.followup.send(
                "上一完整小时没有新帖或作品更新，也未找到可用的推荐作品。请检查分区访问权限与帖子缓存。",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                content="以下为预览，不会发布到播报频道，也不占用今日推荐次数。",
                embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
            )

    @app_commands.command(name="补发小时简报", description="[管理] 补发今天漏掉的简报，默认补最近一个完整小时")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(结束小时="北京时间 0–23；例如填 0 补发零点场，不填则补最近整点")
    async def makeup(self, interaction: discord.Interaction, 结束小时: app_commands.Range[int, 0, 23] = None):
        if (interaction.guild_id != HOURLY_BROADCAST_GUILD_ID
                or not interaction.user.guild_permissions.administrator):
            await interaction.response.send_message("仅限目标服务器的管理员使用。", ephemeral=True)
            return
        now = datetime.now(TZ_SHANGHAI)
        start, end = completed_hour(now)
        if 结束小时 is not None:
            end = end.replace(hour=结束小时)
            if end > now:
                await interaction.response.send_message("只能补发今天已经结束的时段捏～", ephemeral=True)
                return
            start = end - timedelta(hours=1)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            status, message_id = await self.send_window(start, end, makeup=True)
        except Exception:
            log.exception("[小时简报] 管理员补发失败")
            await interaction.followup.send("补发失败，请检查机器人日志中的频道权限或网络错误后重试。", ephemeral=True)
            return
        if status == "empty":
            content = "奇米蛋还没找到可用作品捏，请检查分区访问权限与帖子缓存后再补发～"
        else:
            url = f"https://discord.com/channels/{HOURLY_BROADCAST_GUILD_ID}/{HOURLY_BROADCAST_CHANNEL_ID}/{message_id}"
            content = (f"这个时段已经发过啦，不重复打扰捏～[查看消息]({url})"
                       if status in ("already_sent", "recovered")
                       else f"奇米蛋补发好啦！[查看 {end:%H:%M} 场简报]({url})")
        await interaction.followup.send(content, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
