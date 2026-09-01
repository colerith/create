# protection/modals.py

import discord
from discord import ui
import os
import json
import asyncio
import io
from .utils import check_requirements_common


FILE_TAG_OPTIONS = [
    "角色卡",
    "正则",
    "预设",
    "快速回复",
    "酒馆助手脚本",
    "美化",
    "世界书",
    "其他",
]

FILE_TAG_SET = set(FILE_TAG_OPTIONS)


def normalize_file_tag(tag_text: str | None):
    if tag_text is None:
        return None
    clean_tag = tag_text.strip()
    if not clean_tag:
        return None
    if clean_tag not in FILE_TAG_SET:
        raise ValueError("标签无效，可选值：" + " / ".join(FILE_TAG_OPTIONS))
    return clean_tag


def ensure_filename_with_extension(name_text: str, fallback_filename: str):
    clean_name = name_text.strip()
    if not clean_name:
        raise ValueError("文件名不能为空")

    fallback_ext = os.path.splitext(fallback_filename)[1]
    if fallback_ext and not os.path.splitext(clean_name)[1]:
        return f"{clean_name}{fallback_ext}"
    return clean_name


class DraftTitleModal(ui.Modal, title="设置标题"):
    title_input = ui.TextInput(label="标题", placeholder="请输入...", max_length=100)

    def __init__(self, view):
        super().__init__()
        self.view_ref = view
        self.title_input.default = view.draft_title

    async def on_submit(self, i: discord.Interaction):
        self.view_ref.draft_title = i.data["components"][0]["components"][0]["value"]
        await self.view_ref.update_dashboard(i)


class DraftNoteModal(ui.Modal, title="设置作者提示"):
    log_input = ui.TextInput(
        label="说明/日志",
        style=discord.TextStyle.paragraph,
        placeholder="写点什么...",
        max_length=4000,
        required=False,
    )

    def __init__(self, view):
        super().__init__()
        self.view_ref = view
        self.log_input.default = view.draft_log[:4000] if view.draft_log else None

    async def on_submit(self, i: discord.Interaction):
        self.view_ref.draft_log = i.data["components"][0]["components"][0]["value"]
        await self.view_ref.update_dashboard(i)


class DraftUpdateLogModal(ui.Modal, title="配置更新日志"):
    update_log_input = ui.TextInput(
        label="更新日志 (支持 Markdown)",
        style=discord.TextStyle.paragraph,
        placeholder="请输入更新内容...",
        max_length=4000,
        required=False,
    )
    attachment_input = ui.Label(
        text="更新日志附件",
        description="可选，最多上传 10 个文件；新上传的文件会替换草稿中已有的更新日志附件。",
        component=ui.FileUpload(
            custom_id="draft_update_log_attachments",
            required=False,
            min_values=0,
            max_values=10,
        ),
    )

    def __init__(self, view):
        super().__init__()
        self.view_ref = view
        self.update_log_input.default = (
            view.draft_update_log[:4000] if view.draft_update_log else None
        )

    async def on_submit(self, i: discord.Interaction):
        update_log = self.update_log_input.value.strip()
        uploaded_attachments = list(self.attachment_input.component.values)

        if uploaded_attachments:
            await i.response.defer()
            cached_attachments = []
            try:
                for attachment in uploaded_attachments:
                    cached_attachments.append(
                        DraftUpdateLogAttachment(
                            filename=attachment.filename,
                            data=await attachment.read(),
                            description=getattr(attachment, "description", None),
                            spoiler=attachment.is_spoiler(),
                        )
                    )
            except Exception as exc:
                return await i.followup.send(
                    f"读取更新日志附件失败：{exc}", ephemeral=True
                )
            self.view_ref.draft_update_attachments = cached_attachments
        elif not update_log:
            # 文本与本次上传均为空时，视为关闭整条更新日志。
            self.view_ref.draft_update_attachments = []

        self.view_ref.draft_update_log = update_log or None
        await self.view_ref.update_dashboard(i)


class DraftUpdateLogAttachment:
    """缓存 modal 上传的文件，供稍后的正式发布复用。"""

    def __init__(self, filename, data, description=None, spoiler=False):
        self.filename = filename
        self.data = data
        self.description = description
        self.spoiler = spoiler

    def to_file(self):
        return discord.File(
            io.BytesIO(self.data),
            filename=self.filename,
            description=self.description,
            spoiler=self.spoiler,
        )


class DraftPasswordModal(ui.Modal, title="设置口令"):
    pwd_input = ui.TextInput(
        label="下载口令", placeholder="1-100字", min_length=1, max_length=100
    )

    def __init__(self, view, next_mode):
        super().__init__()
        self.view_ref = view
        self.next_mode = next_mode
        self.pwd_input.default = view.draft_password

    async def on_submit(self, i: discord.Interaction):
        clean_pwd = i.data["components"][0]["components"][0]["value"].strip()
        if not clean_pwd:
            return await i.response.send_message("口令不能为空！", ephemeral=True)
        self.view_ref.draft_password = clean_pwd
        self.view_ref.draft_mode = self.next_mode
        await self.view_ref.update_dashboard(i)


class RenameFileModal(ui.Modal, title="重命名文件"):
    name_input = ui.TextInput(
        label="新文件名 (无需输入后缀)",
        placeholder="例如：我的汉化补丁",
        max_length=100,
    )

    def __init__(self, view_ref, file_index, old_filename):
        super().__init__()
        self.view_ref = view_ref
        self.file_index = file_index
        self.name_stem, self.ext = os.path.splitext(old_filename)
        self.name_input.default = self.name_stem

    async def on_submit(self, interaction: discord.Interaction):
        new_stem = self.name_input.value.strip()
        if not new_stem:
            return await interaction.response.send_message(
                "文件名不能为空！", ephemeral=True
            )
        new_full_name = f"{new_stem}{self.ext}"
        if hasattr(self.view_ref, "file_entries"):
            self.view_ref.file_entries[self.file_index]["filename"] = new_full_name
            if hasattr(self.view_ref, "sync_custom_names_from_entries"):
                self.view_ref.sync_custom_names_from_entries()
        else:
            self.view_ref.custom_names[self.file_index] = new_full_name
        await interaction.response.defer(ephemeral=True)
        await self.view_ref.update_dashboard(interaction)
        await interaction.followup.send(
            f"✅ 文件已重命名为：`{new_full_name}`", ephemeral=True
        )


class PasswordUnlockModal(ui.Modal, title="请输入口令"):
    password_input = ui.TextInput(label="口令", placeholder="请输入...", max_length=50)

    def __init__(self, correct_password, item_row, bot, unlock_type):
        super().__init__()
        self.c = correct_password
        self.row = item_row
        self.bot = bot
        self.ut = unlock_type

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        from .views import register_discord_rate_limit

        if register_discord_rate_limit(self.bot, error, "附件口令验证"):
            return
        await super().on_error(interaction, error)

    async def on_submit(self, i: discord.Interaction):
        user_input = self.password_input.value.strip()
        if user_input != self.c:
            return await i.response.send_message("❌ 口令错误！", ephemeral=True)

        await i.response.defer(ephemeral=True, thinking=True)
        success, msg = await check_requirements_common(
            i, self.ut, self.row["owner_id"], self.row["message_id"]
        )
        if not success:
            return await i.followup.send(msg, ephemeral=True)

        # 【关键修复】在此处延迟导入，解决循环依赖
        from .views import start_download_flow, check_rate_limit_and_report

        allowed, reject_msg = await check_rate_limit_and_report(i, self.bot, self.row)
        if not allowed:
            return await i.followup.send(reject_msg, ephemeral=True)

        await start_download_flow(i, self.bot, self.row)
