# protection/modals.py

import discord
from discord import ui
import os
import json
import asyncio
import re
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


def build_bulk_edit_text(file_entries):
    lines = []
    for idx, entry in enumerate(file_entries, start=1):
        tag = entry.get("tag") or ""
        lines.append(f"{idx}. {entry['filename']} | {tag}")
    return "\n".join(lines)


def apply_bulk_edit_text(file_entries, raw_text: str):
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("请至少保留一行文件配置")

    index_pattern = re.compile(r"^(\d+)\s*[\.、\-)]?\s*(.*)$")
    touched = set()

    for line in lines:
        match = index_pattern.match(line)
        if not match:
            raise ValueError(f"无法解析这一行：{line}")

        file_index = int(match.group(1)) - 1
        if not 0 <= file_index < len(file_entries):
            raise ValueError(f"文件序号超出范围：{file_index + 1}")

        content = match.group(2).strip()
        if "|" in content:
            name_text, tag_text = content.split("|", 1)
        else:
            name_text, tag_text = content, None

        entry = file_entries[file_index]
        entry["filename"] = ensure_filename_with_extension(
            name_text.strip(), entry.get("original_filename") or entry["filename"]
        )
        entry["tag"] = normalize_file_tag(tag_text)
        touched.add(file_index)

    return len(touched)


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

    def __init__(self, view):
        super().__init__()
        self.view_ref = view
        self.update_log_input.default = (
            view.draft_update_log[:4000] if view.draft_update_log else None
        )

    async def on_submit(self, i: discord.Interaction):
        self.view_ref.draft_update_log = i.data["components"][0]["components"][0][
            "value"
        ]
        await self.view_ref.update_dashboard(i)


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


class DraftBulkFileEditModal(ui.Modal, title="批量编辑文件"):
    entries_input = ui.TextInput(
        label="每行: 序号. 文件名 | 标签",
        style=discord.TextStyle.paragraph,
        placeholder=(
            "1. 角色卡-主包.png | 角色卡\n"
            "2. 正则合集.json | 正则\n"
            "标签可选: 角色卡 / 正则 / 预设 / 快速回复 / 酒馆助手脚本 / 美化 / 世界书 / 其他"
        ),
        max_length=4000,
    )

    def __init__(self, view_ref):
        super().__init__()
        self.view_ref = view_ref
        self.entries_input.default = build_bulk_edit_text(view_ref.file_entries)[:4000]

    async def on_submit(self, interaction: discord.Interaction):
        try:
            changed_count = apply_bulk_edit_text(
                self.view_ref.file_entries, self.entries_input.value
            )
        except ValueError as exc:
            return await interaction.response.send_message(f"❌ {exc}", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        await self.view_ref.update_dashboard(interaction)
        await interaction.followup.send(
            f"✅ 已批量更新 {changed_count} 个文件的名称/标签。",
            ephemeral=True,
        )


class PublishedBulkFileEditModal(ui.Modal, title="批量编辑已发布文件"):
    entries_input = ui.TextInput(
        label="每行: 序号. 文件名 | 标签",
        style=discord.TextStyle.paragraph,
        placeholder=(
            "1. 角色卡-主包.png | 角色卡\n"
            "2. 正则合集.json | 正则\n"
            "标签可选: 角色卡 / 正则 / 预设 / 快速回复 / 酒馆助手脚本 / 美化 / 世界书 / 其他"
        ),
        max_length=4000,
    )

    def __init__(self, message_id, file_data):
        super().__init__()
        self.message_id = message_id
        self.file_data = file_data
        self.entries_input.default = build_bulk_edit_text(file_data)[:4000]

    async def on_submit(self, interaction: discord.Interaction):
        try:
            changed_count = apply_bulk_edit_text(
                self.file_data, self.entries_input.value
            )
        except ValueError as exc:
            return await interaction.response.send_message(f"❌ {exc}", ephemeral=True)

        from ..core.db import get_db

        async with get_db() as db:
            await db.execute(
                "UPDATE protected_items SET storage_urls = ? WHERE message_id = ?",
                (json.dumps(self.file_data), self.message_id),
            )
            await db.commit()

        await interaction.response.send_message(
            f"✅ 已批量更新 {changed_count} 个已发布文件的名称/标签。",
            ephemeral=True,
        )


class PasswordUnlockModal(ui.Modal, title="请输入口令"):
    password_input = ui.TextInput(label="口令", placeholder="请输入...", max_length=50)

    def __init__(self, correct_password, item_row, bot, unlock_type):
        super().__init__()
        self.c = correct_password
        self.row = item_row
        self.bot = bot
        self.ut = unlock_type

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
        from .views import start_download_flow

        await start_download_flow(i, self.bot, self.row)
