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
from ..recommend.utils import get_random_thread_pool
from . import db
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
    def is_public_channel(channel):
        if channel is None or channel.guild.id != HOURLY_BROADCAST_GUILD_ID:
            return False
        if isinstance(channel, discord.Thread) and channel.is_private():
            return False
        subject = channel.parent if isinstance(channel, discord.Thread) else channel
        if subject is None or subject.guild.me is None:
            return False
        # 公开简报不将隐藏分区的标题/链接带到公开频道。
        return (subject.permissions_for(subject.guild.default_role).view_channel
                and subject.permissions_for(subject.guild.me).view_channel)

    @commands.Cog.listener()
    async def on_thread_create(self, thread):
        if (HOURLY_BROADCAST_ENABLED and isinstance(thread.parent, discord.ForumChannel)
                and self.is_public_channel(thread) and thread.created_at):
            await db.record_thread(thread)

    async def collect(self, guild, start, end):
        forums = {forum.id: forum for forum in guild.forums if self.is_public_channel(forum)}
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
                checked_channels[channel_id] = (self.is_public_channel(channel) if channel
                                                else channel_id in known_threads)
            if checked_channels[channel_id]:
                updates[channel_id] = row
        return threads, list(updates.values()), cached, forums

    async def daily_report_url(self, guild):
        for channel_id in EXPLORATION_TARGET_CHANNEL_IDS:
            if not self.is_public_channel(guild.get_channel(channel_id)):
                continue
            message_id = await get_panel_message_id(channel_id, "daily_report")
            if message_id:
                return f"https://discord.com/channels/{guild.id}/{channel_id}/{message_id}"
        return None

    async def prepare_embed(self, guild, start, end):
        threads, updates, cached, forums = await self.collect(guild, start, end)
        if not threads and not updates:
            return None, False

        recommendation = None
        # 以推送时段的结束日期计每日额度；午夜的简报属于新一天。
        date = end.date().isoformat()
        if not await db.recommendation_sent(guild.id, HOURLY_BROADCAST_CHANNEL_ID, date):
            pool = [thread for thread in await get_random_thread_pool(guild)
                    if thread.parent_id in forums and self.is_public_channel(thread)]
            if pool:
                recommendation = random.choice(pool)
        embed = build_embed(
            guild, start, end, threads, updates, cached, len(forums),
            recommendation=recommendation, daily_url=await self.daily_report_url(guild),
        )
        return embed, recommendation is not None

    async def find_sent_message(self, channel, end):
        # 处理“Discord 已发送成功，但写数据库前进程退出/连接超时”的情况。
        # 使用机器人作者、固定标题和小时结束时间共同识别，避免重试重复推送。
        date = end.date().isoformat()
        already_recommended = await db.recommendation_sent(channel.guild.id, channel.id, date)
        scan_start = end if already_recommended else end.replace(hour=0)
        async for message in channel.history(after=scan_start, before=end + timedelta(hours=1), limit=None):
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
                        and scan_start <= sent_end < end
                        and sent_end.minute == sent_end.second == sent_end.microsecond == 0):
                    # 若发送后立即重启，下次整点先恢复前一个时段的日推额度。
                    await db.mark_processed(channel.guild.id, channel.id, sent_end, message.id, date)
                    already_recommended = True
        return None

    async def send_window(self, start, end):
        async with self._send_lock:
            if await db.is_processed(HOURLY_BROADCAST_GUILD_ID, HOURLY_BROADCAST_CHANNEL_ID, end):
                return
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
                    log.info("[小时简报] %s 无新帖或更新，跳过", end.isoformat())
                    return
                message = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                message_id = message.id
            await db.mark_processed(
                guild.id, channel.id, end, message_id,
                end.date().isoformat() if has_recommendation else None,
            )
            log.info("[小时简报] %s 已推送至 %s，消息 %s", end.isoformat(), channel.id, message_id)

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
            await interaction.followup.send("上一完整小时没有新帖或作品更新，本时段会跳过推送。", ephemeral=True)
        else:
            await interaction.followup.send(
                content="以下为预览，不会发布到播报频道，也不占用今日推荐次数。",
                embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
            )
