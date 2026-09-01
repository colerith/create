from __future__ import annotations

import tempfile
import zipfile
from datetime import datetime, time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import CORE_BACKUP_CHANNEL_ID, TZ_SHANGHAI


class CoreBackupCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.project_root = Path(__file__).resolve().parents[2]
        self.backup_files = [
            self.project_root / "chimidan.db",
            self.project_root / "config.py",
        ]
        self.daily_core_backup_task.start()

    async def cog_unload(self):
        self.daily_core_backup_task.cancel()

    def _build_backup_archive(self) -> Path:
        timestamp = datetime.now(TZ_SHANGHAI).strftime("%Y%m%d_%H%M%S")
        archive_name = f"core_backup_{timestamp}.zip"
        archive_path = Path(tempfile.gettempdir()) / archive_name

        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            found_files = []
            for file_path in self.backup_files:
                if file_path.exists():
                    archive.write(file_path, arcname=file_path.name)
                    found_files.append(file_path.name)

            manifest = "\n".join(
                [
                    f"generated_at={datetime.now(TZ_SHANGHAI).isoformat()}",
                    "timezone=Asia/Shanghai",
                    f"included={', '.join(found_files) if found_files else 'none'}",
                ]
            )
            archive.writestr("manifest.txt", manifest)

        return archive_path

    def _get_upload_limit_bytes(
        self, channel: discord.TextChannel | discord.Thread
    ) -> int:
        guild = getattr(channel, "guild", None)
        if guild and getattr(guild, "filesize_limit", None):
            return guild.filesize_limit
        return 8 * 1024 * 1024

    def _split_file(self, file_path: Path, chunk_size: int) -> list[Path]:
        part_paths: list[Path] = []
        with file_path.open("rb") as src:
            index = 1
            while True:
                chunk = src.read(chunk_size)
                if not chunk:
                    break
                part_path = file_path.with_name(f"{file_path.name}.part{index:03d}")
                part_path.write_bytes(chunk)
                part_paths.append(part_path)
                index += 1
        return part_paths

    async def send_core_backup(self) -> str:
        channel = self.bot.get_channel(CORE_BACKUP_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(CORE_BACKUP_CHANNEL_ID)
            except (discord.NotFound, discord.Forbidden) as exc:
                print(f"❌ [CoreBackup] 无法访问目标频道 {CORE_BACKUP_CHANNEL_ID}: {exc}")
                return f"无法访问目标频道：{exc}"

        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            print(f"❌ [CoreBackup] 目标频道 {CORE_BACKUP_CHANNEL_ID} 不是可发送消息的文字频道或帖子频道。")
            return "目标频道不是可发送消息的文字频道或帖子频道。"

        archive_path = self._build_backup_archive()
        extra_paths: list[Path] = []
        try:
            filename = archive_path.name
            upload_limit = self._get_upload_limit_bytes(channel)
            archive_size = archive_path.stat().st_size

            if archive_size <= upload_limit:
                await channel.send(
                    content=f"🗂️ 核心数据备份已生成：`{filename}`",
                    file=discord.File(archive_path, filename=filename),
                )
                print(f"✅ [CoreBackup] 已发送核心备份到频道 {CORE_BACKUP_CHANNEL_ID}: {filename}")
                return f"已发送备份：`{filename}`"

            chunk_size = max(1, upload_limit - 64 * 1024)
            part_paths = self._split_file(archive_path, chunk_size)
            extra_paths.extend(part_paths)

            await channel.send(
                content=(
                    f"🗂️ 核心数据备份过大，已自动分卷发送：`{filename}`\n"
                    f"共 `{len(part_paths)}` 个分卷，请全部下载后按顺序合并。"
                )
            )

            for index, part_path in enumerate(part_paths, start=1):
                await channel.send(
                    content=f"备份分卷 {index}/{len(part_paths)}：`{part_path.name}`",
                    file=discord.File(part_path, filename=part_path.name),
                )

            print(
                f"✅ [CoreBackup] 备份超过上传限制，已分卷发送到频道 {CORE_BACKUP_CHANNEL_ID}: {filename} ({len(part_paths)} parts)"
            )
            return f"备份过大，已分卷发送，共 {len(part_paths)} 个文件。"
        finally:
            archive_path.unlink(missing_ok=True)
            for extra_path in extra_paths:
                extra_path.unlink(missing_ok=True)

    @tasks.loop(time=time(hour=4, minute=0, tzinfo=TZ_SHANGHAI))
    async def daily_core_backup_task(self):
        scheduler = getattr(self.bot, "discord_request_scheduler", None)
        if scheduler:
            scheduler.set_current_priority(20)
        print("⏰ [CoreBackup] 开始执行每日核心数据备份任务...")
        await self.send_core_backup()

    @daily_core_backup_task.before_loop
    async def before_daily_core_backup_task(self):
        await self.bot.wait_until_ready()
        print("👍 [CoreBackupCog] Bot 已就绪，每日核心备份任务待命。")

    @app_commands.command(name="发送核心备份", description="[管理] 立即发送一份核心数据备份到备份频道")
    @app_commands.default_permissions(manage_guild=True)
    async def manual_send_core_backup(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("仅限管理员使用。", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        result = await self.send_core_backup()
        await interaction.followup.send(
            f"✅ {result}\n目标频道：<#{CORE_BACKUP_CHANNEL_ID}>",
            ephemeral=True,
        )
