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

    async def send_core_backup(self) -> None:
        channel = self.bot.get_channel(CORE_BACKUP_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(CORE_BACKUP_CHANNEL_ID)
            except (discord.NotFound, discord.Forbidden) as exc:
                print(f"❌ [CoreBackup] 无法访问目标频道 {CORE_BACKUP_CHANNEL_ID}: {exc}")
                return

        if not isinstance(channel, discord.TextChannel):
            print(f"❌ [CoreBackup] 目标频道 {CORE_BACKUP_CHANNEL_ID} 不是文字频道。")
            return

        archive_path = self._build_backup_archive()
        try:
            filename = archive_path.name
            await channel.send(
                content=f"🗂️ 核心数据备份已生成：`{filename}`",
                file=discord.File(archive_path, filename=filename),
            )
            print(f"✅ [CoreBackup] 已发送核心备份到频道 {CORE_BACKUP_CHANNEL_ID}: {filename}")
        finally:
            archive_path.unlink(missing_ok=True)

    @tasks.loop(time=time(hour=4, minute=0, tzinfo=TZ_SHANGHAI))
    async def daily_core_backup_task(self):
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
        await self.send_core_backup()
        await interaction.followup.send(
            f"✅ 已尝试将核心数据备份发送到频道 <#{CORE_BACKUP_CHANNEL_ID}>。",
            ephemeral=True,
        )
