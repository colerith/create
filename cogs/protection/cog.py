# cogs/protection/cog.py

import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timedelta
import json
import asyncio
import random

# --- 从新的地方导入 ---
from config import (
    TZ_SHANGHAI,
    DAILY_DOWNLOAD_LIMIT,
    SUSPICIOUS_WINDOW_MINUTES,
    SUSPICIOUS_MIN_DOWNLOADS,
    SUSPICIOUS_LIST_PAGE_SIZE,
)
from . import db as protection_db
from .utils import is_valid_comment, extract_trace_from_bytes
from .views import (
    ProtectionDraftView,
    PostListView,
    PostSelectionView,
    BumpButtonView,
    UploadSessionControlView,
    UPLOAD_SESSION_TIMEOUT_SECONDS,
    CachedAttachment,
)


class ProtectionCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.ctx_menu = app_commands.ContextMenu(
            name="转为保护附件", callback=self.convert_to_protected
        )
        self.bump_enable_ctx_menu = app_commands.ContextMenu(
            name="激活置底消息", callback=self.ctx_enable_bump
        )
        self.bump_disable_ctx_menu = app_commands.ContextMenu(
            name="关闭置底消息", callback=self.ctx_disable_bump
        )
        self.bump_refresh_ctx_menu = app_commands.ContextMenu(
            name="刷新置底消息", callback=self.ctx_refresh_bump
        )
        self.bot.tree.add_command(self.ctx_menu)
        self.bot.tree.add_command(self.bump_enable_ctx_menu)
        self.bot.tree.add_command(self.bump_disable_ctx_menu)
        self.bot.tree.add_command(self.bump_refresh_ctx_menu)
        self.bump_tasks = {}
        self.upload_sessions = {}

    maker_group = app_commands.Group(
        name="贴主", description="[贴主] 附件保护发布与管理工具"
    )
    user_group = app_commands.Group(
        name="保护附件", description="[用户] 下载与查询附件"
    )
    admin_group = app_commands.Group(
        name="管理员专用", description="[管理] 系统维护工具"
    )

    async def cog_load(self):
        # 注意: 视图注册的逻辑移到了 main.py 的 setup_hook，这里只恢复置底任务
        self.bot.loop.create_task(self._restore_bump_tasks_after_ready())
        print("⏳ [ProtectionCog] 已安排置底任务恢复计划（将在 Bot 就绪后执行）...")

    async def _restore_bump_tasks_after_ready(self):
        await self.bot.wait_until_ready()
        print("🔄 [ProtectionCog] Bot 已就绪，开始恢复置底任务...")
        try:
            # 直接从数据库函数获取配置
            rows = await protection_db.get_all_bump_configs()
            count = 0
            for row in rows:
                channel_id = row["channel_id"]
                channel = self.bot.get_channel(channel_id)
                if not channel:
                    try:
                        channel = await self.bot.fetch_channel(channel_id)
                    except:
                        pass

                if channel and self._is_thread_archived(channel):
                    await protection_db.remove_bump_config(channel_id)
                    continue

                if channel and (channel.id not in self.bump_tasks):
                    task = self.bot.loop.create_task(self._bump_loop(channel))
                    self.bump_tasks[channel_id] = task
                    count += 1
                    await asyncio.sleep(0.15)
                elif not channel:
                    print(
                        f"⚠️ [警告] 彻底无法找到频道 {channel_id}，已自动清理置底配置。"
                    )
                    await protection_db.remove_bump_config(channel_id)

            print(f"✅ [ProtectionCog] 自动置底任务恢复完成！共恢复 {count} 个频道。")
        except Exception as e:
            print(f"❌ [ProtectionCog] 恢复任务失败: {e}")

    async def cog_unload(self):
        self.bot.tree.remove_command(self.ctx_menu.name, type=self.ctx_menu.type)
        self.bot.tree.remove_command(
            self.bump_enable_ctx_menu.name, type=self.bump_enable_ctx_menu.type
        )
        self.bot.tree.remove_command(
            self.bump_disable_ctx_menu.name, type=self.bump_disable_ctx_menu.type
        )
        self.bot.tree.remove_command(
            self.bump_refresh_ctx_menu.name, type=self.bump_refresh_ctx_menu.type
        )
        for task in self.bump_tasks.values():
            task.cancel()

    def _is_thread_archived(
        self, channel: discord.TextChannel | discord.Thread
    ) -> bool:
        return isinstance(channel, discord.Thread) and getattr(
            channel, "archived", False
        )

    def _is_bump_message(self, message: discord.Message) -> bool:
        if message.author.id != self.bot.user.id or not message.components:
            return False

        try:
            first_child = message.components[0].children[0]
        except (AttributeError, IndexError):
            return False

        return getattr(first_child, "custom_id", None) == "bump_get_attachments"

    def _should_repost_stale_bump(
        self, bump_message: discord.Message | None
    ) -> bool:
        """置底按钮超过 2 小时且已离开最近 10 条时，才允许重发。"""
        if not bump_message:
            return False
        created_at = bump_message.created_at
        if created_at is None:
            return False
        now_utc = datetime.now(created_at.tzinfo)
        return (now_utc - created_at) >= timedelta(hours=2)

    def _should_refresh_in_place(self, bump_message: discord.Message | None) -> bool:
        """只要旧按钮还在最近 10 条内，就只编辑原消息。"""
        return bool(bump_message)

    async def _scan_bump_messages(
        self, channel: discord.TextChannel | discord.Thread, limit: int = 10
    ):
        latest_message = None
        old_bump_message = None

        async for msg in channel.history(limit=limit):
            if latest_message is None:
                latest_message = msg

            if old_bump_message is None:
                if self._is_bump_message(msg):
                    old_bump_message = msg
                    continue

        return latest_message, old_bump_message

    async def _get_tracked_bump_message(
        self,
        channel: discord.TextChannel | discord.Thread,
        scanned_bump_message: discord.Message | None = None,
    ) -> discord.Message | None:
        if scanned_bump_message:
            return scanned_bump_message

        message_id = await protection_db.get_sticky_message_id(channel.id)
        if not message_id:
            return None

        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            await protection_db.remove_sticky_message(channel.id)
            return None
        except discord.HTTPException:
            return None

        if not self._is_bump_message(message):
            await protection_db.remove_sticky_message(channel.id)
            return None
        return message

    async def _send_bump_message(self, channel: discord.TextChannel | discord.Thread):
        view = BumpButtonView(self.bot)
        message = await channel.send(**view.create_layout())
        await protection_db.set_sticky_message(channel.id, message.id)
        return message

    async def _refresh_bump_message(self, message: discord.Message) -> bool:
        try:
            view = BumpButtonView(self.bot)
            await message.edit(**view.create_layout())
            return True
        except discord.NotFound:
            return False
        except discord.HTTPException:
            return False

    async def _safe_delete_message(self, message: discord.Message) -> bool:
        try:
            await message.delete()
            return True
        except discord.HTTPException as e:
            # 50083: Thread is archive
            if getattr(e, "code", None) == 50083:
                return False
            raise

    # --- 监听器 ---
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.user_id != self.bot.user.id:
            await protection_db.add_like(payload.user_id, payload.message_id)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        await protection_db.remove_like(payload.user_id, payload.message_id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        await self._collect_upload_session_message(message)

        if not isinstance(message.channel, discord.Thread):
            return

        if is_valid_comment(message.content):
            await protection_db.add_or_update_comment(
                message.author.id, message.channel.id, message.content
            )

    def _session_key(self, user_id: int, channel_id: int):
        return (user_id, channel_id)

    def get_upload_session(self, user_id: int, channel_id: int):
        return self.upload_sessions.get(self._session_key(user_id, channel_id))

    async def _collect_upload_session_message(self, message: discord.Message):
        if not message.attachments:
            return

        key = self._session_key(message.author.id, message.channel.id)
        session = self.upload_sessions.get(key)
        if not session:
            return

        now = datetime.now(TZ_SHANGHAI)
        if now >= session["expires_at"]:
            self.upload_sessions.pop(key, None)
            return

        if any(saved.id == message.id for saved in session["messages"]):
            return

        session["messages"].append(message)
        print(
            f"[ProtectionDebug] upload-session-collected: user={message.author.id} channel={message.channel.id} message={message.id} attachments={len(message.attachments)}"
        )

        try:
            await message.reply(
                "🔒 已收纳到保护附件草稿，待贴主确认发布后才会转为正式保护附件。",
                mention_author=False,
                delete_after=20,
            )
        except Exception:
            pass

    async def _expire_upload_session(
        self, user_id: int, channel_id: int, expires_at: datetime
    ):
        delay = max((expires_at - datetime.now(TZ_SHANGHAI)).total_seconds(), 0)
        try:
            await asyncio.sleep(delay)
            key = self._session_key(user_id, channel_id)
            session = self.upload_sessions.get(key)
            if not session or session["expires_at"] != expires_at:
                return
            self.upload_sessions.pop(key, None)
            print(
                f"[ProtectionDebug] upload-session-expired: user={user_id} channel={channel_id} messages={len(session['messages'])}"
            )
        except asyncio.CancelledError:
            pass

    async def cancel_upload_session(
        self,
        interaction: discord.Interaction,
        user_id: int,
        channel_id: int,
        reason: str,
    ):
        key = self._session_key(user_id, channel_id)
        session = self.upload_sessions.pop(key, None)
        if session and session.get("task"):
            session["task"].cancel()
        await interaction.response.edit_message(content=reason, embed=None, view=None)

    async def finish_upload_session(
        self, interaction: discord.Interaction, user_id: int, channel_id: int
    ):
        key = self._session_key(user_id, channel_id)
        session = self.upload_sessions.pop(key, None)
        if not session:
            return await interaction.response.edit_message(
                content="❌ 当前没有可提交的附件收集会话。",
                embed=None,
                view=None,
            )

        if session.get("task"):
            session["task"].cancel()

        attachments = []
        default_log_parts = []
        source_messages = session["messages"]
        for msg in source_messages:
            for att in msg.attachments:
                try:
                    file_bytes = await att.read()
                except Exception as exc:
                    print(
                        f"[ProtectionDebug] upload-session-read-source-failed: message={msg.id} attachment={getattr(att, 'id', None)} error={exc}"
                    )
                    return await interaction.response.edit_message(
                        content=f"❌ 收纳附件失败：{exc}",
                        embed=None,
                        view=None,
                    )

                attachments.append(
                    CachedAttachment(
                        filename=att.filename,
                        title=getattr(att, "title", None),
                        data=file_bytes,
                        content_type=getattr(att, "content_type", None),
                        size=getattr(att, "size", None),
                    )
                )
            if msg.content:
                default_log_parts.append(msg.content)

        if not attachments:
            return await interaction.response.edit_message(
                content="❌ 还没有收集到任何附件，请先发送带附件的消息。",
                embed=None,
                view=None,
            )

        for msg in source_messages:
            try:
                await msg.delete()
            except Exception as exc:
                print(
                    f"[ProtectionDebug] upload-session-delete-source-failed: message={msg.id} error={exc}"
                )

        target_message = None
        default_log = "\n\n".join(default_log_parts).strip() or None
        view = ProtectionDraftView(
            self.bot,
            interaction.user,
            attachments,
            target_message=target_message,
            default_log=default_log,
        )
        embed = discord.Embed(title="🚀 正在启动保护向导...", color=0x87CEEB)
        await interaction.response.edit_message(content=None, embed=embed, view=view)
        await view.update_dashboard(interaction)

    # --- 管理员命令 ---
    @admin_group.command(name="修复面板", description="移除本频道所有旧面板的按钮")
    async def fix_panels(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        rows = await protection_db.get_items_in_channel(
            interaction.channel.id, limit=999
        )  # 获取所有
        if not rows:
            return await interaction.followup.send(
                "本频道在数据库中没有活跃记录。", ephemeral=True
            )

        success_count, fail_count = 0, 0
        for row in rows:
            try:
                msg = await interaction.channel.fetch_message(row["message_id"])
                await msg.edit(view=None)
                success_count += 1
                await asyncio.sleep(0.5)
            except:
                fail_count += 1
        await interaction.followup.send(
            f"✅ 修复完成！\n已移除按钮的消息: {success_count} 个\n失败/已删除: {fail_count} 个",
            ephemeral=True,
        )

    @admin_group.command(name="可疑下载名单", description="查询短时间高频下载的可疑用户名单")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(page="页码，从 1 开始")
    async def suspicious_download_list(
        self, interaction: discord.Interaction, page: app_commands.Range[int, 1, 999] = 1
    ):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message(
                "❌ 仅管理员可使用此命令。", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        since_dt = datetime.now(TZ_SHANGHAI) - timedelta(minutes=SUSPICIOUS_WINDOW_MINUTES)
        since_iso = since_dt.isoformat()

        total_users = await protection_db.count_suspicious_users_in_window(
            since_iso, SUSPICIOUS_MIN_DOWNLOADS
        )
        if total_users == 0:
            return await interaction.followup.send(
                f"✅ 最近 {SUSPICIOUS_WINDOW_MINUTES} 分钟内暂无达到阈值（{SUSPICIOUS_MIN_DOWNLOADS} 次）的用户。",
                ephemeral=True,
            )

        page_size = SUSPICIOUS_LIST_PAGE_SIZE
        total_pages = max(1, (total_users + page_size - 1) // page_size)
        if page > total_pages:
            page = total_pages

        offset = (page - 1) * page_size
        rows = await protection_db.get_suspicious_users_in_window(
            since_iso,
            SUSPICIOUS_MIN_DOWNLOADS,
            limit=page_size,
            offset=offset,
        )

        embed = discord.Embed(
            title="🚨 短时高频下载可疑名单",
            color=discord.Color.orange(),
            description=(
                f"统计窗口: 最近 {SUSPICIOUS_WINDOW_MINUTES} 分钟\n"
                f"触发阈值: ≥ {SUSPICIOUS_MIN_DOWNLOADS} 次下载"
            ),
        )
        lines = []
        for idx, row in enumerate(rows, start=offset + 1):
            user_id = row["user_id"]
            download_count = row["download_count"]
            unique_posts = row["unique_posts"]
            latest_ts = row["latest_timestamp"]
            try:
                latest_fmt = discord.utils.format_dt(
                    datetime.fromisoformat(latest_ts), "T"
                )
            except Exception:
                latest_fmt = latest_ts
            lines.append(
                f"{idx}. <@{user_id}> (`{user_id}`) | 次数: **{download_count}** | 帖子: **{unique_posts}** | 最近: {latest_fmt}"
            )

        embed.add_field(name="名单", value="\n".join(lines)[:1024], inline=False)
        embed.set_footer(text=f"第 {page}/{total_pages} 页 | 总人数 {total_users}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @admin_group.command(name="溯源", description="检查文件水印并查询下载记录")
    @app_commands.describe(file="请上传需要检查的文件")
    async def trace_file(
        self, interaction: discord.Interaction, file: discord.Attachment
    ):
        await interaction.response.defer(ephemeral=True)
        file_bytes = await file.read()
        trace_id = extract_trace_from_bytes(file_bytes, file.filename)

        if not trace_id:
            return await interaction.followup.send(
                "⚠️ **未检测到溯源信息**\n文件可能非机器人分发，或水印已被破坏。",
                ephemeral=True,
            )

        record = await protection_db.get_trace_record(trace_id)
        if not record:
            return await interaction.followup.send(
                f"⚠️ **检测到水印但数据丢失无法匹配！**\nTraceID: `{trace_id}`",
                ephemeral=True,
            )

        user_text = f"<@{record['user_id']}> ({record['user_id']})"
        dl_time = discord.utils.format_dt(datetime.fromisoformat(record["created_at"]))
        embed = discord.Embed(
            title="🔍 溯源报告",
            color=0xFF0000,
            description=f"**文件**: `{record['filename']}`\n**溯源 ID**: `{trace_id}`",
        )
        embed.add_field(name="👤 下载者", value=user_text, inline=False)
        embed.add_field(name="📅 下载时间", value=dl_time, inline=True)
        embed.add_field(
            name="📍 来源频道", value=f"<#{record['channel_id']}>", inline=True
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # --- 用户命令 ---
    @user_group.command(name="今日下载记录", description="查询今日下载历史和剩余次数")
    async def my_downloads_today(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        today_start_iso = (
            datetime.now(TZ_SHANGHAI)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .isoformat()
        )
        logs = await protection_db.get_user_downloads_since(
            interaction.user.id, today_start_iso
        )

        remaining = DAILY_DOWNLOAD_LIMIT - len(logs)
        embed = discord.Embed(
            title=f"📜 {interaction.user.display_name} 的今日下载记录",
            color=discord.Color.blue(),
        )
        embed.description = f"**今日下载次数**: {len(logs)}/{DAILY_DOWNLOAD_LIMIT}\n**剩余次数**: {remaining}"

        if logs:
            log_text = ""
            for log in logs:
                try:
                    filenames = ", ".join(json.loads(log["filenames"]))
                except:
                    filenames = "未知"
                ts = discord.utils.format_dt(
                    datetime.fromisoformat(log["timestamp"]), "T"
                )
                log_text += f"- **{log['title']}**: `{filenames}` ({ts})\n"
            embed.add_field(name="详细记录", value=log_text[:1024], inline=False)
        else:
            embed.add_field(name="记录", value="今天还没有下载过任何附件哦。")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @user_group.command(name="获取附件", description="显示本频道最近的受保护附件列表")
    async def get_attachments_list(self, interaction: discord.Interaction):
        rows = await protection_db.get_items_in_channel(
            interaction.channel.id, limit=25
        )  # 下拉菜单最多25个
        if not rows:
            return await interaction.response.send_message(
                "❌ 本频道没有任何受保护的附件记录。", ephemeral=True
            )
        view = PostListView(self.bot, rows)
        embed = discord.Embed(
            title="📂 附件获取列表",
            description=f"发现本频道有 **{len(rows)}** 个最近的附件包。\n请在下方下拉菜单中选择一个下载。",
            color=0x87CEEB,
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    # --- 贴主命令 ---
    @maker_group.command(name="管理附件", description="管理我在此讨论串中发布的附件")
    async def manage_attachments(self, interaction: discord.Interaction):
        if not isinstance(interaction.channel, (discord.Thread, discord.TextChannel)):
            return await interaction.response.send_message(
                "❌  此命令只能在文字频道或论坛帖子中使用。", ephemeral=True
            )

        rows = await protection_db.get_user_items_in_channel(
            interaction.user.id, interaction.channel.id
        )
        if not rows:
            return await interaction.response.send_message(
                "你在当前频道中没有发布过受保护的附件。", ephemeral=True
            )

        posts = [dict(row) for row in rows]
        view = PostSelectionView(self.bot, posts)
        await interaction.response.send_message(
            f"你在此频道中发布了 {len(posts)} 个受保护的附件，请选择进行管理：",
            view=view,
            ephemeral=True,
        )

    async def convert_to_protected(
        self, interaction: discord.Interaction, message: discord.Message
    ):
        if message.author != interaction.user:
            return await interaction.response.send_message(
                "不可以动别人的东西！", ephemeral=True
            )
        if not message.attachments:
            return await interaction.response.send_message(
                "消息里没有附件？", ephemeral=True
            )

        view = ProtectionDraftView(
            self.bot,
            interaction.user,
            message.attachments,
            target_message=message,
            default_log=message.content or None,
        )
        embed = discord.Embed(title="🚀 正在启动保护向导...", color=0x87CEEB)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        await view.update_dashboard(interaction)

    @maker_group.command(
        name="发布保护附件", description="开启 5 分钟附件收集并创建保护贴"
    )
    async def create_protection(self, interaction: discord.Interaction):
        key = self._session_key(interaction.user.id, interaction.channel.id)
        old_session = self.upload_sessions.pop(key, None)
        if old_session and old_session.get("task"):
            old_session["task"].cancel()

        expires_at = datetime.now(TZ_SHANGHAI) + timedelta(
            seconds=UPLOAD_SESSION_TIMEOUT_SECONDS
        )
        task = self.bot.loop.create_task(
            self._expire_upload_session(
                interaction.user.id, interaction.channel.id, expires_at
            )
        )
        self.upload_sessions[key] = {
            "messages": [],
            "expires_at": expires_at,
            "task": task,
        }

        print(
            f"[ProtectionDebug] upload-session-started: user={interaction.user.id} channel={interaction.channel.id} expires_at={expires_at.isoformat()}"
        )

        view = UploadSessionControlView(
            self, interaction.user.id, interaction.channel.id
        )
        await interaction.response.send_message(
            embed=view._build_embed(),
            view=view,
            ephemeral=True,
        )

    @maker_group.command(
        name="置底附件列表", description="开启/关闭本频道附件列表的自动置底"
    )
    @app_commands.describe(开关="'on'开启, 'off'关闭")
    async def auto_bump(self, interaction: discord.Interaction, 开关: str):
        if not self._can_manage_bump(interaction):
            return await interaction.response.send_message(
                "❌ 只有 **管理员** 或 **贴主** 才能使用。", ephemeral=True
            )

        channel = interaction.channel
        if 开关.lower() == "off":
            ok, msg = await self._disable_auto_bump(channel)
            return await interaction.response.send_message(msg, ephemeral=True)

        if 开关.lower() == "on":
            ok, msg = await self._enable_auto_bump(channel)
            return await interaction.response.send_message(msg, ephemeral=True)

        await interaction.response.send_message("请明确指示 `on` 或 `off`。", ephemeral=True)

    def _can_manage_bump(self, interaction: discord.Interaction) -> bool:
        is_admin = interaction.user.guild_permissions.administrator
        is_owner = (
            isinstance(interaction.channel, discord.Thread)
            and interaction.channel.owner_id == interaction.user.id
        )
        return bool(is_admin or is_owner)

    async def _disable_auto_bump(
        self, channel: discord.TextChannel | discord.Thread
    ) -> tuple[bool, str]:
        channel_id = channel.id
        if channel_id in self.bump_tasks:
            self.bump_tasks[channel_id].cancel()
            del self.bump_tasks[channel_id]

        await protection_db.remove_bump_config(channel_id)
        await protection_db.remove_sticky_message(channel_id)

        try:
            async for msg in channel.history(limit=20):
                if self._is_bump_message(msg):
                    await msg.delete()
                    break
        except Exception:
            pass

        return True, "✅ 已**关闭**本频道的自动置底，并尝试清理了旧面板。"

    async def _enable_auto_bump(
        self, channel: discord.TextChannel | discord.Thread
    ) -> tuple[bool, str]:
        channel_id = channel.id
        rows = await protection_db.get_items_in_channel(channel_id, limit=1)
        if not rows:
            return False, "❌ 本频道没有任何受保护的附件记录，无法开启置底。"

        if channel_id in self.bump_tasks:
            self.bump_tasks[channel_id].cancel()

        task = self.bot.loop.create_task(self._bump_loop(channel))
        self.bump_tasks[channel_id] = task
        await protection_db.add_bump_config(channel_id)
        await self._execute_bump_once(channel)
        return True, "✅ **自动置底已开启**\nBot 将定时检查并维护置底面板，首次面板已发送。"

    async def _refresh_auto_bump(
        self, channel: discord.TextChannel | discord.Thread
    ) -> tuple[bool, str]:
        rows = await protection_db.get_items_in_channel(channel.id, limit=1)
        if not rows:
            return False, "❌ 本频道没有任何受保护的附件记录，无法刷新置底。"
        await self._execute_bump_once(channel)
        return True, "🔄 已执行一次置底刷新。"

    def _resolve_bump_target_channel(
        self, message: discord.Message
    ) -> discord.TextChannel | discord.Thread | None:
        if isinstance(message.channel, (discord.TextChannel, discord.Thread)):
            return message.channel
        return None

    async def ctx_enable_bump(
        self, interaction: discord.Interaction, message: discord.Message
    ):
        if not self._can_manage_bump(interaction):
            return await interaction.response.send_message(
                "❌ 只有 **管理员** 或 **贴主** 才能使用。", ephemeral=True
            )
        channel = self._resolve_bump_target_channel(message)
        if not channel:
            return await interaction.response.send_message(
                "❌ 当前消息所在频道不支持置底功能。", ephemeral=True
            )
        ok, text = await self._enable_auto_bump(channel)
        await interaction.response.send_message(text, ephemeral=True)

    async def ctx_disable_bump(
        self, interaction: discord.Interaction, message: discord.Message
    ):
        if not self._can_manage_bump(interaction):
            return await interaction.response.send_message(
                "❌ 只有 **管理员** 或 **贴主** 才能使用。", ephemeral=True
            )
        channel = self._resolve_bump_target_channel(message)
        if not channel:
            return await interaction.response.send_message(
                "❌ 当前消息所在频道不支持置底功能。", ephemeral=True
            )
        ok, text = await self._disable_auto_bump(channel)
        await interaction.response.send_message(text, ephemeral=True)

    async def ctx_refresh_bump(
        self, interaction: discord.Interaction, message: discord.Message
    ):
        if not self._can_manage_bump(interaction):
            return await interaction.response.send_message(
                "❌ 只有 **管理员** 或 **贴主** 才能使用。", ephemeral=True
            )
        channel = self._resolve_bump_target_channel(message)
        if not channel:
            return await interaction.response.send_message(
                "❌ 当前消息所在频道不支持置底功能。", ephemeral=True
            )
        ok, text = await self._refresh_auto_bump(channel)
        await interaction.response.send_message(text, ephemeral=True)

    async def _execute_bump_once(self, channel: discord.TextChannel | discord.Thread):
        """单独执行一次置底循环的逻辑，用于提供即时反馈"""
        try:
            if self._is_thread_archived(channel):
                await protection_db.remove_bump_config(channel.id)
                if channel.id in self.bump_tasks:
                    self.bump_tasks.pop(channel.id, None)
                return
            should_have_bump = bool(
                await protection_db.get_items_in_channel(channel.id, limit=1)
            )
            _, nearby_bump_message = await self._scan_bump_messages(channel)
            tracked_bump_message = await self._get_tracked_bump_message(
                channel, nearby_bump_message
            )

            if should_have_bump and not tracked_bump_message:
                await self._send_bump_message(channel)
            elif should_have_bump and tracked_bump_message:
                if self._should_refresh_in_place(nearby_bump_message):
                    await self._refresh_bump_message(tracked_bump_message)
                elif self._should_repost_stale_bump(tracked_bump_message):
                    deleted = await self._safe_delete_message(tracked_bump_message)
                    if not deleted:
                        await protection_db.remove_bump_config(channel.id)
                        await protection_db.remove_sticky_message(channel.id)
                        if channel.id in self.bump_tasks:
                            self.bump_tasks.pop(channel.id, None)
                        return
                    await protection_db.remove_sticky_message(channel.id)
                    await asyncio.sleep(1)
                    await self._send_bump_message(channel)

        except discord.NotFound:
            print(
                f"❌ [置底任务] 频道不存在或无法访问，已自动关闭置底: {getattr(channel, 'id', None)}"
            )
            await protection_db.remove_bump_config(channel.id)
            await protection_db.remove_sticky_message(channel.id)
            if channel.id in self.bump_tasks:
                self.bump_tasks.pop(channel.id, None)
        except discord.Forbidden:
            print(
                f"❌ [置底任务] 没有权限访问频道，已自动关闭置底: {getattr(channel, 'id', None)}"
            )
            await protection_db.remove_bump_config(channel.id)
            await protection_db.remove_sticky_message(channel.id)
            if channel.id in self.bump_tasks:
                self.bump_tasks.pop(channel.id, None)
        except Exception as e:
            print(f"- [置底任务] {channel.name} 首次执行中发生错误: {e}")

    async def _bump_loop(self, channel: discord.TextChannel | discord.Thread):
        try:
            # 在循环开始前等待一小段时间，确保cog完全加载
            await asyncio.sleep(5 + random.uniform(0, 8))
            while not self.bot.is_closed():
                try:
                    if self._is_thread_archived(channel):
                        await protection_db.remove_bump_config(channel.id)
                        if channel.id in self.bump_tasks:
                            del self.bump_tasks[channel.id]
                        break
                    # 1.【核心】每次循环都检查数据库，确认是否还需要置底
                    rows = await protection_db.get_items_in_channel(channel.id, limit=1)
                    should_have_bump = bool(rows)

                    # 2. 查找旧的置底消息
                    _, nearby_bump_message = await self._scan_bump_messages(
                        channel
                    )
                    tracked_bump_message = await self._get_tracked_bump_message(
                        channel, nearby_bump_message
                    )

                    # 3. 根据状态执行正确的操作 (状态机逻辑)
                    if should_have_bump:
                        if not tracked_bump_message:
                            await self._send_bump_message(channel)
                        elif self._should_refresh_in_place(nearby_bump_message):
                            await self._refresh_bump_message(tracked_bump_message)
                        elif self._should_repost_stale_bump(tracked_bump_message):
                            deleted = await self._safe_delete_message(tracked_bump_message)
                            if not deleted:
                                await protection_db.remove_bump_config(channel.id)
                                await protection_db.remove_sticky_message(channel.id)
                                if channel.id in self.bump_tasks:
                                    del self.bump_tasks[channel.id]
                                break
                            await asyncio.sleep(1)
                            await protection_db.remove_sticky_message(channel.id)
                            await self._send_bump_message(channel)

                    else:  # not should_have_bump
                        # **情况E：不该有，但有了 -> 删除它，并自动停止任务**
                        if tracked_bump_message:
                            await self._safe_delete_message(tracked_bump_message)
                            await protection_db.remove_sticky_message(channel.id)

                        # 自动关闭
                        print(
                            f"ℹ️ [置底任务] 频道 {channel.name} 已无附件，自动停止置底任务。"
                        )
                        if channel.id in self.bump_tasks:
                            del self.bump_tasks[channel.id]
                        await protection_db.remove_bump_config(channel.id)
                        await protection_db.remove_sticky_message(channel.id)
                        break  # 跳出 while True 循环，终止此任务

                except discord.NotFound:
                    print(
                        f"❌ [置底任务] 频道不存在或无法访问，任务为频道 {getattr(channel, 'name', 'unknown')} 自动停止。"
                    )
                    await protection_db.remove_bump_config(channel.id)
                    await protection_db.remove_sticky_message(channel.id)
                    if channel.id in self.bump_tasks:
                        del self.bump_tasks[channel.id]
                    break
                except discord.Forbidden:
                    print(
                        f"❌ [置底任务] 权限不足，任务为频道 {channel.name} 自动停止。"
                    )
                    await protection_db.remove_bump_config(channel.id)
                    await protection_db.remove_sticky_message(channel.id)
                    if channel.id in self.bump_tasks:
                        del self.bump_tasks[channel.id]
                    break  # 没权限了直接退出循环
                except discord.HTTPException as e:
                    if getattr(e, "code", None) == 50083:
                        await protection_db.remove_bump_config(channel.id)
                        await protection_db.remove_sticky_message(channel.id)
                        if channel.id in self.bump_tasks:
                            del self.bump_tasks[channel.id]
                        break
                    import traceback

                    print(f"- [缃簳浠诲姟] {channel.name} HTTPException: {e}")
                    traceback.print_exc()
                except Exception as e:
                    import traceback

                    print(f"- [置底任务] {channel.name} 循环中发生错误: {e}")
                    traceback.print_exc()

                # 无论如何都等待
                await asyncio.sleep(300 + random.uniform(0, 30))  # 5分钟检查一次

        except asyncio.CancelledError:
            # 这是正常的任务取消
            pass
