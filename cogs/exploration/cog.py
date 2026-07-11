# cogs/exploration/cog.py

import asyncio
import json
import math
import re
from datetime import datetime

import discord
from discord import app_commands, ui
from discord.ext import commands, tasks

from config import EXPLORATION_TARGET_CHANNEL_IDS, ADMIN_USER_ID, TZ_SHANGHAI
from . import db as exploration_db
from .views import (
    DownloadLibraryContainer,
    MyWorksContainer,
    SearchPanelContainer,
    SearchResultContainer,
    UpdateSummaryContainer,
)
from ..core.db import get_panel_message_id, set_panel_message_id, remove_panel_record
from ..protection import db as protection_db


DOWNLOAD_RECORDS_PER_PAGE = 10


class DownloadRecordQueryModal(ui.Modal, title="查询下载记录"):
    keyword = ui.TextInput(
        label="用户名或数字 ID",
        placeholder="支持昵称、用户名或纯数字 ID",
        min_length=1,
        max_length=100,
    )

    def __init__(self, cog: "ExplorationCog"):
        super().__init__(timeout=180)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        keyword = str(self.keyword.value).strip()
        target = await self.cog.resolve_download_target_user(interaction.guild, keyword)
        if not target:
            return await interaction.followup.send(
                "未找到匹配用户，请输入更精确的昵称/用户名，或直接输入数字 ID。",
                ephemeral=True,
            )

        target_user_id, target_display_name = target
        total_count = await protection_db.count_user_download_logs(target_user_id)
        if total_count <= 0:
            return await interaction.followup.send(
                f"用户 **{target_display_name}**（`{target_user_id}`）暂无下载记录。",
                ephemeral=True,
            )

        view = DownloadRecordResultView(
            invoker_id=interaction.user.id,
            guild_id=interaction.guild_id,
            target_user_id=target_user_id,
            target_display_name=target_display_name,
            total_count=total_count,
        )
        embed = await view.build_embed()
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


class DownloadRecordResultView(ui.View):
    def __init__(
        self,
        invoker_id: int,
        guild_id: int,
        target_user_id: int,
        target_display_name: str,
        total_count: int,
    ):
        super().__init__(timeout=300)
        self.invoker_id = invoker_id
        self.guild_id = guild_id
        self.target_user_id = target_user_id
        self.target_display_name = target_display_name
        self.total_count = total_count

        self.per_page = DOWNLOAD_RECORDS_PER_PAGE
        self.current_page = 0
        self.total_pages = max(1, math.ceil(self.total_count / self.per_page))
        self._sync_buttons()

    def _sync_buttons(self):
        self.btn_prev.disabled = self.current_page <= 0
        self.btn_next.disabled = self.current_page >= self.total_pages - 1
        self.btn_page.label = f"{self.current_page + 1} / {self.total_pages}"

    @staticmethod
    def _clip(text: str, limit: int) -> str:
        text = "" if text is None else str(text)
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 1)] + "…"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("这个查询面板只能由命令发起者操作。", ephemeral=True)
            return False
        return True

    async def build_embed(self) -> discord.Embed:
        offset = self.current_page * self.per_page
        rows = await protection_db.get_user_download_logs_page(
            self.target_user_id,
            limit=self.per_page,
            offset=offset,
        )

        embed = discord.Embed(
            title="下载记录查询",
            color=discord.Color.blurple(),
            description=(
                f"目标用户：<@{self.target_user_id}>（`{self.target_user_id}`）\n"
                f"显示名称：**{self.target_display_name}**\n"
                f"总记录：**{self.total_count}**"
            ),
        )

        if not rows:
            embed.add_field(name="记录", value="当前页没有数据。", inline=False)
            return embed

        lines = []
        start_index = offset + 1
        for idx, row in enumerate(rows, start=start_index):
            channel_id = row["channel_id"]
            message_id = row["message_id"]
            jump_url = (
                f"https://discord.com/channels/{self.guild_id}/{channel_id}/{message_id}"
                if channel_id
                else None
            )

            try:
                files = json.loads(row["filenames"] or "[]")
                files_text = ", ".join(files[:3]) if files else "无文件名"
                if len(files) > 3:
                    files_text += f" 等{len(files)}个"
            except Exception:
                files_text = "文件名解析失败"

            title = row["title"] or "(无标题)"
            ts_raw = row["timestamp"] or ""
            try:
                ts_text = discord.utils.format_dt(datetime.fromisoformat(ts_raw), style="f")
            except Exception:
                ts_text = ts_raw or "未知时间"

            link_text = f"[原帖跳转]({jump_url})" if jump_url else "原帖链接不可用"
            safe_title = self._clip(title, 80)
            safe_files = self._clip(files_text, 120)
            line = f"**{idx}. {safe_title}**\n文件: `{safe_files}`\n时间: {ts_text} | {link_text}"
            lines.append(self._clip(line, 240))

        joined = "\n\n".join(lines)
        if len(joined) > 1000:
            joined = joined[:997] + "..."
        embed.add_field(name="记录明细", value=joined, inline=False)
        return embed

    @ui.button(label="上一页", style=discord.ButtonStyle.secondary)
    async def btn_prev(self, interaction: discord.Interaction, button: ui.Button):
        if self.current_page > 0:
            self.current_page -= 1
        self._sync_buttons()
        embed = await self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    @ui.button(label="1 / 1", style=discord.ButtonStyle.secondary, disabled=True)
    async def btn_page(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()

    @ui.button(label="下一页", style=discord.ButtonStyle.secondary)
    async def btn_next(self, interaction: discord.Interaction, button: ui.Button):
        if self.current_page < self.total_pages - 1:
            self.current_page += 1
        self._sync_buttons()
        embed = await self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)


class ExplorationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.add_view(SearchPanelContainer(self.bot))
        self.daily_task.start()
        self.pushed_panel_refresh_task.start()

    async def cog_unload(self):
        self.daily_task.cancel()
        self.pushed_panel_refresh_task.cancel()

    def _is_admin(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == ADMIN_USER_ID or interaction.user.guild_permissions.administrator

    @staticmethod
    def _row_to_dict(row) -> dict:
        return {key: row[key] for key in row.keys()}

    def _resolve_item_channel(self, guild: discord.Guild, channel_id: int | None):
        return guild.get_channel(channel_id) if guild and channel_id else None

    async def _decorate_work_rows(self, guild: discord.Guild, rows, selected_channel_ids: list[int] | None = None):
        selected = set(selected_channel_ids or [])
        decorated = []
        for row in rows:
            data = self._row_to_dict(row)
            channel = self._resolve_item_channel(guild, data.get("channel_id"))
            parent = getattr(channel, "parent", None)
            if selected:
                channel_matches = data.get("channel_id") in selected
                parent_matches = getattr(parent, "id", None) in selected
                if not channel_matches and not parent_matches:
                    continue

            tags = []
            if isinstance(channel, discord.Thread):
                tags = [tag.name for tag in getattr(channel, "applied_tags", [])[:4]]
            data["tags"] = " ".join(tags) if tags else "无标签"
            decorated.append(data)
        return decorated

    async def _build_download_library_view(
        self,
        user: discord.User | discord.Member,
        guild: discord.Guild,
        timeout: int | None = 300,
    ):
        rows = await protection_db.get_user_library_items(user.id)
        return DownloadLibraryContainer(
            [self._row_to_dict(row) for row in rows],
            title="📥 下载记录",
            user=user,
            guild=guild,
            bot=self.bot,
            timeout=timeout,
        )

    async def _build_my_works_view(
        self,
        user: discord.User | discord.Member,
        guild: discord.Guild,
        selected_channel_ids: list[int] | None = None,
        timeout: int | None = 300,
    ):
        rows = await protection_db.get_user_published_items(user.id)
        decorated = await self._decorate_work_rows(guild, rows, selected_channel_ids)
        suffix = "（已筛选）" if selected_channel_ids else ""
        return MyWorksContainer(
            decorated,
            title=f"📚 我的作品{suffix}",
            user=user,
            guild=guild,
            bot=self.bot,
            selected_channel_ids=selected_channel_ids,
            timeout=timeout,
        )

    async def send_download_library_panel(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        view = await self._build_download_library_view(interaction.user, interaction.guild)
        await interaction.followup.send(view=view, ephemeral=True)

    async def send_my_works_panel(self, interaction: discord.Interaction, selected_channel_ids: list[int] | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        view = await self._build_my_works_view(interaction.user, interaction.guild, selected_channel_ids)
        await interaction.followup.send(view=view, ephemeral=True)

    async def _fetch_push_user_and_guild(self, user_id: int, guild_id: int):
        guild = self.bot.get_guild(guild_id)
        user = guild.get_member(user_id) if guild else None
        if user is None:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
        return user, guild

    async def _build_pushed_view(self, panel_type: str, user, guild, selected_channel_ids=None):
        if panel_type == "download_library":
            return await self._build_download_library_view(user, guild, timeout=None)
        if panel_type == "my_works":
            return await self._build_my_works_view(user, guild, selected_channel_ids, timeout=None)
        raise ValueError(f"未知面板类型: {panel_type}")

    async def push_panel_to_dm(
        self,
        interaction: discord.Interaction,
        panel_type: str,
        user_id: int,
        selected_channel_ids: list[int] | None = None,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if interaction.user.id != user_id:
            return await interaction.followup.send("只能推送自己的面板。", ephemeral=True)

        user, guild = await self._fetch_push_user_and_guild(user_id, interaction.guild_id)
        if not guild:
            return await interaction.followup.send("无法定位服务器，暂时不能创建私信推送。", ephemeral=True)

        view = await self._build_pushed_view(panel_type, user, guild, selected_channel_ids)
        dm = await user.create_dm()
        record = await exploration_db.get_panel_push(user_id, guild.id, panel_type, "dm")
        sent_msg = None
        if record:
            try:
                sent_msg = await dm.fetch_message(record["message_id"])
                await sent_msg.edit(view=view)
            except discord.NotFound:
                sent_msg = None
        if sent_msg is None:
            sent_msg = await dm.send(view=view)

        now = datetime.now(TZ_SHANGHAI).isoformat()
        await exploration_db.upsert_panel_push(
            user_id,
            guild.id,
            panel_type,
            "dm",
            sent_msg.id,
            now,
            filter_channel_ids=json.dumps(selected_channel_ids or []),
        )
        await interaction.followup.send("已推送到私信；之后会定期编辑刷新同一条私信面板。", ephemeral=True)

    async def delete_dm_panel_push(self, interaction: discord.Interaction, panel_type: str, user_id: int):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if interaction.user.id != user_id:
            return await interaction.followup.send("只能删除自己的私信推送。", ephemeral=True)
        record = await exploration_db.get_panel_push(user_id, interaction.guild_id, panel_type, "dm")
        if record:
            try:
                dm = await interaction.user.create_dm()
                msg = await dm.fetch_message(record["message_id"])
                await msg.delete()
            except discord.NotFound:
                pass
            await exploration_db.remove_panel_push(user_id, interaction.guild_id, panel_type, "dm")
        await interaction.followup.send("私信推送已删除，记录也已清除。", ephemeral=True)

    @staticmethod
    def _parse_channel_id(raw: str) -> int | None:
        raw = raw.strip()
        match = re.search(r"/channels/\d+/(\d+)", raw)
        if match:
            return int(match.group(1))
        digits = re.search(r"\d{15,25}", raw)
        return int(digits.group(0)) if digits else None

    async def push_my_works_to_channel(
        self,
        interaction: discord.Interaction,
        user_id: int,
        channel_input: str,
        selected_channel_ids: list[int] | None = None,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if interaction.user.id != user_id:
            return await interaction.followup.send("只能推送自己的作品面板。", ephemeral=True)

        channel_id = self._parse_channel_id(channel_input)
        if not channel_id:
            return await interaction.followup.send("没有识别到有效频道链接或频道 ID。", ephemeral=True)

        target = self.bot.get_channel(channel_id)
        if target is None:
            try:
                target = await self.bot.fetch_channel(channel_id)
            except Exception:
                target = None
        if not target or not hasattr(target, "send"):
            return await interaction.followup.send("找不到可发送消息的目标频道。", ephemeral=True)
        if getattr(getattr(target, "guild", None), "id", None) != interaction.guild_id:
            return await interaction.followup.send("只能推送到当前服务器内的频道。", ephemeral=True)
        perms = target.permissions_for(interaction.guild.me)
        if not perms.send_messages:
            return await interaction.followup.send("我没有在该频道发送消息的权限。", ephemeral=True)

        guild = interaction.guild
        view = await self._build_my_works_view(interaction.user, guild, selected_channel_ids, timeout=None)
        record = await exploration_db.get_panel_push(user_id, guild.id, "my_works", "channel", channel_id)
        sent_msg = None
        if record:
            try:
                sent_msg = await target.fetch_message(record["message_id"])
                await sent_msg.edit(view=view)
            except discord.NotFound:
                sent_msg = None
        if sent_msg is None:
            sent_msg = await target.send(view=view)

        now = datetime.now(TZ_SHANGHAI).isoformat()
        await exploration_db.upsert_panel_push(
            user_id,
            guild.id,
            "my_works",
            "channel",
            sent_msg.id,
            now,
            target_channel_id=channel_id,
            filter_channel_ids=json.dumps(selected_channel_ids or []),
        )
        await interaction.followup.send("作品面板已推送到指定频道；之后会定期编辑刷新旧面板。", ephemeral=True)

    async def get_todays_threads(self, guild: discord.Guild) -> list[discord.Thread]:
        """获取今天发布的所有帖子。"""
        today_start_ts = datetime.now(TZ_SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        threads_list: list[discord.Thread] = []
        for forum in guild.forums:
            if not forum.permissions_for(guild.me).read_messages:
                continue
            threads_list.extend([t for t in forum.threads if t.created_at and t.created_at.timestamp() >= today_start_ts])
        threads_list.sort(key=lambda t: t.created_at or datetime.min, reverse=True)
        return threads_list

    async def get_todays_update_logs(self, guild: discord.Guild):
        """获取今天发布过更新日志的作品记录。"""
        today_start = datetime.now(TZ_SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        return await protection_db.get_attachment_update_publish_logs_since(
            today_start,
            guild_id=guild.id,
        )

    async def refresh_channel_update_summary_panel(self, channel: discord.TextChannel, resend: bool = False):
        """刷新日报下方的更新汇总面板。"""
        rows = await self.get_todays_update_logs(channel.guild)
        view = UpdateSummaryContainer(
            rows,
            title="✨ 更新汇总",
            user=self.bot.user,
            guild=channel.guild,
        )
        panel_type = "daily_update_summary"
        message_id = await get_panel_message_id(channel.id, panel_type)

        if message_id and not resend:
            try:
                target_msg = await channel.fetch_message(message_id)
                await target_msg.edit(view=view)
                return
            except discord.NotFound:
                await remove_panel_record(channel.id, panel_type)
            except Exception as e:
                print(f"编辑数据库记录的更新汇总面板({message_id})失败: {e}")

        if resend and message_id:
            try:
                old_msg = await channel.fetch_message(message_id)
                await old_msg.delete()
            except discord.NotFound:
                pass
            finally:
                await remove_panel_record(channel.id, panel_type)

        new_msg = await channel.send(view=view)
        await set_panel_message_id(channel.id, new_msg.id, panel_type)

    async def refresh_channel_daily_panel(self, channel: discord.TextChannel, resend: bool = False):
        """刷新频道日报面板。"""
        threads = await self.get_todays_threads(channel.guild)
        date_str = datetime.now(TZ_SHANGHAI).strftime("%Y年%m月%d日")
        panel_title = f"📮 {date_str} 更新日报"
        view = SearchResultContainer(threads, title=panel_title, user=self.bot.user, is_daily=True)

        message_id = await get_panel_message_id(channel.id, "daily_report")

        if message_id and not resend:
            try:
                target_msg = await channel.fetch_message(message_id)
                await target_msg.edit(view=view)
                await self.refresh_channel_update_summary_panel(channel, resend=False)
                return
            except discord.NotFound:
                await remove_panel_record(channel.id, "daily_report")
            except Exception as e:
                print(f"编辑数据库记录的日报面板({message_id})失败: {e}")

        if resend and message_id:
            try:
                old_msg = await channel.fetch_message(message_id)
                await old_msg.delete()
            except discord.NotFound:
                pass
            finally:
                await remove_panel_record(channel.id, "daily_report")

        summary_message_id = await get_panel_message_id(channel.id, "daily_update_summary")
        if resend and summary_message_id:
            try:
                old_summary_msg = await channel.fetch_message(summary_message_id)
                await old_summary_msg.delete()
            except discord.NotFound:
                pass
            finally:
                await remove_panel_record(channel.id, "daily_update_summary")

        if resend:
            try:
                async for msg in channel.history(limit=20):
                    if msg.author == self.bot.user and msg.components:
                        is_daily_report = False
                        for comp in msg.components[0].children:
                            if isinstance(comp, discord.ui.Container) and comp.accent_colour == discord.Color.gold():
                                is_daily_report = True
                                break
                        if is_daily_report:
                            await msg.delete()
            except Exception:
                pass

        new_msg = await channel.send(view=view)
        await set_panel_message_id(channel.id, new_msg.id, "daily_report")
        await self.refresh_channel_update_summary_panel(channel, resend=False)

    @tasks.loop(minutes=10)
    async def daily_task(self):
        """自动刷新日报面板。"""
        for channel_id in EXPLORATION_TARGET_CHANNEL_IDS:
            if channel := self.bot.get_channel(channel_id):
                await self.refresh_channel_daily_panel(channel, resend=False)

    @daily_task.before_loop
    async def before_daily_task(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=30)
    async def pushed_panel_refresh_task(self):
        """定期编辑刷新用户推送出去的探索面板。"""
        rows = await exploration_db.get_all_panel_pushes()
        for row in rows:
            try:
                selected_channel_ids = json.loads(row["filter_channel_ids"] or "[]")
            except Exception:
                selected_channel_ids = []

            try:
                user, guild = await self._fetch_push_user_and_guild(row["user_id"], row["guild_id"])
                if not guild:
                    continue
                view = await self._build_pushed_view(row["panel_type"], user, guild, selected_channel_ids)

                if row["delivery_type"] == "dm":
                    target = await user.create_dm()
                    remove_target_channel_id = None
                else:
                    target = self.bot.get_channel(row["target_channel_id"])
                    if target is None:
                        try:
                            target = await self.bot.fetch_channel(row["target_channel_id"])
                        except Exception:
                            target = None
                    remove_target_channel_id = row["target_channel_id"]

                if not target:
                    await exploration_db.remove_panel_push(
                        row["user_id"],
                        row["guild_id"],
                        row["panel_type"],
                        row["delivery_type"],
                        remove_target_channel_id,
                    )
                    continue

                try:
                    msg = await target.fetch_message(row["message_id"])
                    await msg.edit(view=view)
                    await exploration_db.upsert_panel_push(
                        row["user_id"],
                        row["guild_id"],
                        row["panel_type"],
                        row["delivery_type"],
                        row["message_id"],
                        datetime.now(TZ_SHANGHAI).isoformat(),
                        target_channel_id=remove_target_channel_id,
                        filter_channel_ids=json.dumps(selected_channel_ids),
                    )
                except discord.NotFound:
                    await exploration_db.remove_panel_push(
                        row["user_id"],
                        row["guild_id"],
                        row["panel_type"],
                        row["delivery_type"],
                        remove_target_channel_id,
                    )
            except Exception as e:
                print(f"刷新探索推送面板失败: {e}")

    @pushed_panel_refresh_task.before_loop
    async def before_pushed_panel_refresh_task(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="更新日报面板", description="[管理] 强制刷新并重发本频道日报面板")
    @app_commands.default_permissions(administrator=True)
    async def manual_daily_report(self, interaction: discord.Interaction):
        if not self._is_admin(interaction):
            return await interaction.response.send_message("你没有权限执行此命令。", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        if interaction.channel_id in EXPLORATION_TARGET_CHANNEL_IDS:
            await self.refresh_channel_daily_panel(interaction.channel, resend=True)
            await interaction.followup.send("日报面板已强制重发最新版。", ephemeral=True)
        else:
            await interaction.followup.send("此命令只能在已配置的日报频道使用。", ephemeral=True)

    @app_commands.command(name="更新搜索面板", description="[管理] 清理旧面板并发送新的搜索面板")
    @app_commands.default_permissions(administrator=True)
    async def refresh_search_panel(self, interaction: discord.Interaction):
        if not self._is_admin(interaction):
            return await interaction.response.send_message("你没有权限执行此命令。", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        try:
            async for msg in interaction.channel.history(limit=50):
                if msg.author == self.bot.user and msg.components:
                    is_search_panel = False
                    for comp in msg.components[0].children:
                        if isinstance(comp, discord.ui.Container):
                            for item in comp.children:
                                if isinstance(item, discord.ui.ActionRow):
                                    for btn in item.children:
                                        if getattr(btn, "custom_id", None) == "search_panel_btn_keyword_v2":
                                            is_search_panel = True
                                            break
                                if is_search_panel:
                                    break
                        if is_search_panel:
                            break
                    if is_search_panel:
                        await msg.delete()
                        await asyncio.sleep(0.5)
        except Exception as e:
            print(f"清理搜索面板失败: {e}")

        await interaction.channel.send(view=SearchPanelContainer(self.bot))
        await interaction.followup.send("最新的搜索面板已部署。", ephemeral=True)

    @app_commands.command(name="查询下载记录", description="[管理] 查询用户通过本Bot下载的所有记录")
    @app_commands.default_permissions(administrator=True)
    async def query_download_records(self, interaction: discord.Interaction):
        if not self._is_admin(interaction):
            return await interaction.response.send_message("你没有权限执行此命令。", ephemeral=True)
        await interaction.response.send_modal(DownloadRecordQueryModal(self))

    async def resolve_download_target_user(self, guild: discord.Guild, keyword: str) -> tuple[int, str] | None:
        keyword = keyword.strip()
        if not keyword:
            return None

        if keyword.isdigit():
            user_id = int(keyword)
            member = guild.get_member(user_id)
            display_name = member.display_name if member else str(user_id)
            return user_id, display_name

        keyword_lower = keyword.lower()
        candidates: list[tuple[int, discord.Member]] = []

        def _collect(member: discord.Member):
            names = [member.display_name, member.name]
            if member.global_name:
                names.append(member.global_name)
            lowered = [n.lower() for n in names if n]
            if any(keyword_lower in n for n in lowered):
                score = 0 if any(keyword_lower == n for n in lowered) else 1
                candidates.append((score, member))

        for member in guild.members:
            _collect(member)

        if not candidates:
            try:
                queried = await guild.query_members(query=keyword, limit=25)
            except Exception:
                queried = []
            for member in queried:
                _collect(member)

        if not candidates:
            return None

        candidates.sort(key=lambda item: (item[0], item[1].joined_at or datetime.max))
        chosen = candidates[0][1]
        return chosen.id, chosen.display_name

    @app_commands.command(name="快捷搜索", description="调出快捷搜索面板")
    async def search_cmd(self, interaction: discord.Interaction):
        await interaction.response.send_message(view=SearchPanelContainer(self.bot), ephemeral=True)
