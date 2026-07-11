# protection/views.py

import discord
from discord import ui
import json
import io
import asyncio
import os
import re
import zipfile
import aiosqlite
from urllib.parse import urlparse

from datetime import datetime, timedelta
from ..core.db import get_db
from . import db as protection_db
from .db import log_file_trace
from config import (
    TZ_SHANGHAI,
    BACKUP_CHANNEL_ID,
    DAILY_DOWNLOAD_LIMIT,
    TEST_ROLE_ID,
    DOWNLOAD_RATE_LIMIT_WINDOW_MINUTES,
    DOWNLOAD_RATE_LIMIT_MAX_TIMES,
    DOWNLOAD_RATE_WARNING_DAILY_REPORT_THRESHOLD,
    ABNORMAL_REPORT_CHANNEL_ID,
)
from .utils import (
    fetch_files_common,
    make_discord_files_common,
    check_requirements_common,
    record_download_common,
    inject_smart_trace,
    generate_trace_id,
)
from .modals import (
    DraftTitleModal,
    DraftNoteModal,
    DraftPasswordModal,
    RenameFileModal,
    PasswordUnlockModal,
    DraftUpdateLogModal,
    normalize_file_tag,
)


FILE_EDIT_PAGE_SIZE = 15
UPLOAD_SESSION_TIMEOUT_SECONDS = 300
IMAGE_FILE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".tiff",
    ".tif",
    ".svg",
    ".avif",
    ".heic",
    ".heif",
}
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


class CachedAttachment:
    def __init__(self, filename, title, data, content_type=None, size=None):
        self.filename = filename
        self.title = title
        self._data = data
        self.content_type = content_type
        self.size = size if size is not None else len(data)

    async def read(self):
        return self._data


class ReusableStoredAttachment:
    def __init__(self, stored_entry):
        self.stored_entry = ensure_file_entry_defaults(dict(stored_entry))
        self.filename = self.stored_entry["filename"]
        self.title = self.stored_entry.get("original_filename") or self.filename
        self.content_type = None
        self.size = None

    async def read(self):
        raise RuntimeError("ReusableStoredAttachment does not support direct read")


def has_collectible_message_content(message: discord.Message) -> bool:
    return bool((message.content or "").strip())


def count_collectible_message_items(message: discord.Message) -> int:
    text_count = 1 if has_collectible_message_content(message) and not message.attachments else 0
    return len(message.attachments) + text_count


def _sanitize_virtual_filename(name: str, fallback: str = "text_attachment") -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\r\n\t]', "_", (name or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ._")
    return cleaned[:80] or fallback


def build_text_attachment_name(text: str) -> str:
    content = (text or "").strip()
    if not content:
        return "text_attachment.txt"

    first_line = content.splitlines()[0].strip()
    url_match = re.fullmatch(r"https?://\S+", first_line)
    if url_match and content == first_line:
        parsed = urlparse(first_line)
        path_part = parsed.path.strip("/").replace("/", "_")
        stem_parts = [parsed.netloc]
        if path_part:
            stem_parts.append(path_part)
        stem = "_".join(part for part in stem_parts if part)
        return f"{_sanitize_virtual_filename(stem, 'link')}.txt"

    preview = first_line[:60]
    return f"{_sanitize_virtual_filename(preview)}.txt"


def build_text_cached_attachment(text: str, filename: str | None = None) -> CachedAttachment:
    final_filename = filename or build_text_attachment_name(text)
    text_bytes = text.encode("utf-8")
    attachment = CachedAttachment(
        filename=final_filename,
        title=final_filename,
        data=text_bytes,
        content_type="text/plain; charset=utf-8",
        size=len(text_bytes),
    )
    attachment.inline_text_content = text
    attachment.inline_storage_strategy = "inline_text"
    return attachment


def split_storage_entries(storage_items):
    file_items = []
    text_items = []
    if not isinstance(storage_items, list):
        return file_items, text_items

    for entry in storage_items:
        if not isinstance(entry, dict):
            continue
        if entry.get("strategy") == "inline_text":
            text_items.append(entry)
        else:
            file_items.append(entry)
    return file_items, text_items


def build_text_display_blocks(text_items, limit=8):
    blocks = []
    for idx, entry in enumerate(text_items[:limit], start=1):
        name = entry.get("filename", f"文本 {idx}")
        content = (entry.get("text_content") or "").strip() or "（空内容）"
        preview = content if len(content) <= 900 else content[:900] + "..."
        blocks.append((f"📝 {idx}. {name}", preview))
    return blocks


def is_image_filename(filename: str | None) -> bool:
    if not filename:
        return False
    return os.path.splitext(filename)[1].lower() in IMAGE_FILE_EXTENSIONS


def sanitize_markdown_link_label(label: str | None, fallback: str = "image") -> str:
    value = (label or fallback).replace("[", "(").replace("]", ")")
    return value or fallback


def build_markdown_link_lines(link_items, limit=12):
    lines = []
    for item in link_items[:limit]:
        filename = sanitize_markdown_link_label(item.get("label"))
        url = item.get("url")
        if not url:
            continue
        lines.append(f"[{filename}]({url})")
    return lines


def add_image_download_fields(embed: discord.Embed, image_link_items):
    if not image_link_items:
        return embed

    link_lines = build_markdown_link_lines(image_link_items)
    if not link_lines:
        return embed

    chunks = []
    current_chunk = ""
    for line in link_lines:
        if len(current_chunk) + len(line) + 1 > 1024:
            chunks.append(current_chunk)
            current_chunk = line
        else:
            current_chunk = f"{current_chunk}\n{line}".strip()
    if current_chunk:
        chunks.append(current_chunk)

    for idx, chunk in enumerate(chunks[:4], start=1):
        field_name = "下载方式2：原图浏览器快捷跳转" if idx == 1 else f"图片链接 {idx}"
        embed.add_field(
            name=field_name,
            value=chunk,
            inline=False,
        )

    return embed


def build_image_archive_file(file_pairs, archive_name: str) -> discord.File | None:
    if not file_pairs:
        return None

    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for filename, file_bytes in file_pairs:
            archive.writestr(filename, file_bytes)

    archive_buffer.seek(0)
    return discord.File(archive_buffer, filename=archive_name)


def log_attachment_debug(stage, attachment):
    attr_names = [
        "id",
        "filename",
        "title",
        "description",
        "content_type",
        "size",
        "url",
        "proxy_url",
        "ephemeral",
    ]
    payload = {}
    for name in attr_names:
        try:
            payload[name] = getattr(attachment, name, None)
        except Exception as exc:
            payload[name] = f"<error:{exc}>"

    try:
        if hasattr(attachment, "to_dict"):
            raw_dict = attachment.to_dict()
            payload["raw_keys"] = sorted(raw_dict.keys())
            payload["raw_title"] = raw_dict.get("title")
            payload["raw_filename"] = raw_dict.get("filename")
            payload["raw_description"] = raw_dict.get("description")
    except Exception as exc:
        payload["raw_dict_error"] = str(exc)

    for extra_name in ["_payload", "__dict__"]:
        try:
            extra_value = getattr(attachment, extra_name, None)
            if isinstance(extra_value, dict):
                payload[f"{extra_name}_keys"] = sorted(extra_value.keys())
                payload[f"{extra_name}_title"] = extra_value.get("title")
                payload[f"{extra_name}_filename"] = extra_value.get("filename")
                payload[f"{extra_name}_description"] = extra_value.get("description")
        except Exception as exc:
            payload[f"{extra_name}_error"] = str(exc)

    print(f"[ProtectionDebug] {stage}: {payload}")


def get_attachment_original_name(attachment):
    log_attachment_debug("attachment-name-detect", attachment)
    title = getattr(attachment, "title", None)
    filename = getattr(attachment, "filename", None)
    candidate = title or filename

    if candidate and filename:
        filename_ext = os.path.splitext(filename)[1]
        if filename_ext and not candidate.lower().endswith(filename_ext.lower()):
            candidate = f"{candidate}{filename_ext}"

    print(
        "[ProtectionDebug] attachment-name-result:",
        {
            "chosen_name": candidate or "unknown",
            "title": title,
            "filename": filename,
        },
    )
    return candidate or "unknown"


def ensure_file_entry_defaults(entry):
    entry.setdefault("original_filename", entry.get("filename", "unknown"))
    entry.setdefault("filename", entry.get("original_filename", "unknown"))
    entry["tag"] = normalize_file_tag(entry.get("tag"))
    return entry


def format_tag_text(entry):
    tag = entry.get("tag")
    return f" [{tag}]" if tag else ""


def build_file_line(entry, index=None):
    prefix = f"{index}. " if index is not None else ""
    original_text = ""
    original_name = entry.get("original_filename")
    if original_name and entry["filename"] != original_name:
        original_text = f" (原始: {original_name})"
    return f"{prefix}{entry['filename']}{format_tag_text(entry)}{original_text}"


def get_unlock_mode_text(unlock_type: str, *, rich: bool = False) -> str:
    plain_map = {
        "like": "👍 点赞首楼",
        "like_comment": "👍💬 点赞首楼 + 回复本贴",
        "like_password": "👍🔐 点赞首楼 + 口令",
        "like_comment_password": "👍💬🔐 点赞首楼 + 回复本贴 + 口令",
    }
    rich_map = {
        "like": "👍 **点赞首楼**",
        "like_comment": "👍💬 **点赞首楼 + 回复本贴**",
        "like_password": "👍🔐 **点赞首楼 + 口令**",
        "like_comment_password": "👍💬🔐 **点赞首楼 + 回复本贴 + 口令**",
    }
    mapping = rich_map if rich else plain_map
    return mapping.get(unlock_type, "未知")


def build_protected_post_embed(
    *,
    title: str,
    unlock_type: str,
    file_count: int,
    author_name: str | None,
    author_icon_url: str | None,
    publish_time_text: str,
    bot_avatar_url: str | None,
):
    final_desc = "📋 **本附件受保护，请按照下方指示获取。**"
    embed = discord.Embed(
        title=f"✨ {title}",
        description=final_desc,
        color=discord.Color.from_rgb(255, 183, 197),
    )
    if author_name:
        embed.set_author(name=author_name, icon_url=author_icon_url)
    embed.add_field(
        name="🔑 获取条件",
        value=get_unlock_mode_text(unlock_type, rich=True),
        inline=True,
    )
    embed.add_field(name="📦 文件数量", value=f"**{file_count}** 个", inline=True)
    embed.add_field(name="⏰ 发布时间", value=publish_time_text, inline=True)
    embed.add_field(
        name="📥 如何下载？",
        value="请使用命令：\n**`/保护附件 获取附件`**\n来验证条件并下载文件。",
        inline=False,
    )
    if bot_avatar_url:
        embed.set_footer(text="由 创作保护助手 强力驱动", icon_url=bot_avatar_url)
    else:
        embed.set_footer(text="由 创作保护助手 强力驱动")
    return embed


async def get_protected_item_row(message_id: int):
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM protected_items WHERE message_id = ?",
            (message_id,),
        )
        return await cursor.fetchone()


async def refresh_protected_post_embed(bot, message_id: int):
    row = await get_protected_item_row(message_id)
    if not row:
        return False

    try:
        storage_items = json.loads(row["storage_urls"] or "[]")
    except (TypeError, json.JSONDecodeError):
        storage_items = []
    storage_items = [ensure_file_entry_defaults(item) for item in storage_items if isinstance(item, dict)]
    file_count = len(storage_items)

    channel = bot.get_channel(row["channel_id"])
    if not channel:
        try:
            channel = await bot.fetch_channel(row["channel_id"])
        except Exception:
            return False

    try:
        message = await channel.fetch_message(message_id)
    except Exception:
        return False

    existing_embed = message.embeds[0] if message.embeds else None
    publish_time_text = None
    author_name = None
    author_icon_url = None
    if existing_embed:
        author_name = existing_embed.author.name if existing_embed.author else None
        author_icon_url = existing_embed.author.icon_url if existing_embed.author else None
        for field in existing_embed.fields:
            if field.name == "⏰ 发布时间":
                publish_time_text = field.value
                break

    if not publish_time_text:
        try:
            publish_time_text = discord.utils.format_dt(
                datetime.fromisoformat(row["created_at"])
            )
        except Exception:
            publish_time_text = "未知"

    embed = build_protected_post_embed(
        title=row["title"],
        unlock_type=row["unlock_type"],
        file_count=file_count,
        author_name=author_name,
        author_icon_url=str(author_icon_url) if author_icon_url else None,
        publish_time_text=publish_time_text,
        bot_avatar_url=(
            str(bot.user.display_avatar.url)
            if getattr(bot, "user", None) and getattr(bot.user, "display_avatar", None)
            else None
        ),
    )
    try:
        await message.edit(embed=embed)
    except Exception:
        return False
    return True


async def collect_cached_attachments_from_messages(source_messages):
    attachments = []
    default_log_parts = []
    for msg in source_messages:
        has_text_item = has_collectible_message_content(msg)
        for att in msg.attachments:
            file_bytes = await att.read()
            attachments.append(
                CachedAttachment(
                    filename=att.filename,
                    title=getattr(att, "title", None),
                    data=file_bytes,
                    content_type=getattr(att, "content_type", None),
                    size=getattr(att, "size", None),
                )
            )
        if has_text_item and not msg.attachments:
            attachments.append(build_text_cached_attachment(msg.content.strip()))
        elif msg.content:
            default_log_parts.append(msg.content)
    return attachments, "\n\n".join(default_log_parts).strip() or None


async def build_storage_entries_from_attachments(bot, user, title, attachments, file_entries):
    prepared_files = []
    inline_text_entries = []
    reused_entries = []
    for idx, att in enumerate(attachments):
        entry = ensure_file_entry_defaults(dict(file_entries[idx]))
        stored_entry = getattr(att, "stored_entry", None)
        if stored_entry:
            merged_entry = ensure_file_entry_defaults(dict(stored_entry))
            merged_entry["filename"] = entry["filename"]
            merged_entry["original_filename"] = entry["original_filename"]
            merged_entry["tag"] = entry.get("tag")
            reused_entries.append(merged_entry)
            continue

        file_bytes = await att.read()
        if getattr(att, "inline_storage_strategy", None) == "inline_text":
            inline_text_entries.append(
                {
                    "strategy": "inline_text",
                    "filename": entry["filename"],
                    "original_filename": entry["original_filename"],
                    "tag": entry.get("tag"),
                    "text_content": file_bytes.decode("utf-8", errors="replace"),
                }
            )
        else:
            prepared_files.append((file_bytes, entry["filename"], entry))

    stored_data = []
    if prepared_files:
        stored_data = await upload_files_for_storage(bot, user, title, prepared_files)
    stored_data.extend(inline_text_entries)
    stored_data.extend(reused_entries)
    return stored_data


async def build_storage_entry_from_message(bot, user, post_title, msg, old_tag=None):
    if msg.attachments:
        attachment = msg.attachments[0]
        file_bytes = await attachment.read()
        new_original_name = get_attachment_original_name(attachment)
        stored_data = await upload_files_for_storage(
            bot,
            user,
            post_title,
            [(
                file_bytes,
                new_original_name,
                {
                    "filename": new_original_name,
                    "original_filename": new_original_name,
                    "tag": old_tag,
                },
            )],
        )
        return stored_data[0]

    text_attachment = build_text_cached_attachment(msg.content.strip())
    file_bytes = await text_attachment.read()
    new_original_name = text_attachment.filename
    return ensure_file_entry_defaults(
        {
            "strategy": "inline_text",
            "filename": new_original_name,
            "original_filename": new_original_name,
            "tag": old_tag,
            "text_content": file_bytes.decode("utf-8", errors="replace"),
        }
    )


async def get_user_published_items(user_id: int, limit: int = 25):
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT *
            FROM protected_items
            WHERE owner_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        )
        return await cursor.fetchall()


def get_reusable_attachments_from_rows(posts_rows, message_id: str | int):
    row = next((item for item in posts_rows if str(item["message_id"]) == str(message_id)), None)
    if not row:
        return None, []

    try:
        storage_items = json.loads(row["storage_urls"] or "[]")
    except (TypeError, json.JSONDecodeError):
        storage_items = []
    storage_items = [
        ensure_file_entry_defaults(item)
        for item in storage_items
        if isinstance(item, dict)
    ]
    return row, [ReusableStoredAttachment(item) for item in storage_items]


def build_reusable_metadata_from_row(row):
    if not row:
        return {}
    return {
        "draft_title": row["title"],
        "draft_log": row["log"],
        "draft_update_log": row["update_log"],
        "draft_mode": row["unlock_type"] or "like",
        "mention_users": bool(row["mention_users"]),
        "draft_password": row["password"],
    }


def chunk_file_entries(file_entries, page_index, page_size=FILE_EDIT_PAGE_SIZE):
    start = page_index * page_size
    end = start + page_size
    return start, end, file_entries[start:end]


def apply_rename_lines_to_entries(entries, raw_text, start=None, end=None):
    import re

    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("请至少保留一行文件配置")

    pattern = re.compile(r"^(\d+)\s*[\.、\-)]?\s*(.*)$")
    updated = 0
    for line in lines:
        match = pattern.match(line)
        if not match:
            raise ValueError(f"无法解析这一行：{line}")
        absolute_index = int(match.group(1)) - 1
        if start is not None and end is not None and not start <= absolute_index < end:
            raise ValueError("当前弹窗只能编辑当前页的文件")
        if not 0 <= absolute_index < len(entries):
            raise ValueError(f"文件序号超出范围：{absolute_index + 1}")

        entry = entries[absolute_index]
        new_name = match.group(2).strip()
        if not new_name:
            raise ValueError("文件名不能为空")
        ext = os.path.splitext(entry["original_filename"])[1]
        if ext and not os.path.splitext(new_name)[1]:
            new_name = f"{new_name}{ext}"
        entry["filename"] = new_name
        updated += 1
    return updated


async def upload_files_for_storage(bot, user, title, prepared_files):
    """
    与发布流程一致的存储上传逻辑：优先私信备份，失败则转发到备份频道。
    prepared_files: [(file_bytes, final_filename, entry_dict), ...]
    """
    try:
        dm = await user.create_dm()
        dm_files = [
            discord.File(io.BytesIO(file_bytes), filename=final_filename)
            for file_bytes, final_filename, _entry in prepared_files
        ]
        backup_msg = await dm.send(
            content=f"【{title}】的私信备份！\n(此消息仅作为文件源，请勿删除)",
            files=dm_files,
        )
    except Exception as e:
        print(f"DM backup failed: {e}")
        fallback_channel = bot.get_channel(BACKUP_CHANNEL_ID)
        if not fallback_channel:
            fallback_channel = await bot.fetch_channel(BACKUP_CHANNEL_ID)
        fallback_files = [
            discord.File(io.BytesIO(file_bytes), filename=final_filename)
            for file_bytes, final_filename, _entry in prepared_files
        ]
        backup_msg = await fallback_channel.send(
            content=f"📦 **备用存储** (DM Failed)\nUser: {user} ({user.id})\nTitle: {title}",
            files=fallback_files,
        )

    stored_data = []
    for i, att in enumerate(backup_msg.attachments):
        entry = dict(prepared_files[i][2])
        stored_data.append(
            {
                "strategy": "msg_ref",
                "channel_id": backup_msg.channel.id,
                "message_id": backup_msg.id,
                "attachment_index": i,
                "filename": entry["filename"],
                "original_filename": entry["original_filename"],
                "tag": entry.get("tag"),
                "url": att.url,
            }
        )
    return stored_data


async def deliver_protected_content(
    interaction: discord.Interaction,
    bot,
    row,
    *,
    owner_bypass: bool = False,
):
    raw_items = json.loads(row["storage_urls"])
    storage_items = [ensure_file_entry_defaults(f) for f in raw_items if isinstance(f, dict)]
    file_items, text_items = split_storage_entries(storage_items)

    file_results = []
    if file_items:
        if owner_bypass:
            file_results = await fetch_files_common(bot, file_items)
        else:
            raw_results = await fetch_files_common(bot, file_items)
            guild_id = interaction.guild_id if interaction.guild else 0
            channel_id = interaction.channel_id
            timestamp = datetime.now(TZ_SHANGHAI).isoformat()

            for i, res in enumerate(raw_results):
                correct_filename = file_items[i].get("filename", res["filename"])
                trace_id = generate_trace_id()
                new_bytes = inject_smart_trace(res["bytes"], correct_filename, trace_id)
                await log_file_trace(
                    trace_id=trace_id,
                    user_id=interaction.user.id,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    message_id=row["message_id"],
                    filename=correct_filename,
                    timestamp=timestamp,
                )
                file_results.append(
                    {"filename": correct_filename, "bytes": new_bytes}
                )

    image_archive_pairs = []
    for file_item, file_result in zip(file_items, file_results):
        archive_filename = (
            file_item.get("original_filename")
            or file_item.get("filename")
            or file_result["filename"]
        )
        if is_image_filename(archive_filename):
            image_archive_pairs.append((archive_filename, file_result["bytes"]))

    embed = None
    text_blocks = build_text_display_blocks(text_items)
    if text_blocks:
        embed = discord.Embed(
            title="📝 文本内容",
            color=discord.Color.teal(),
        )
        for title, preview in text_blocks:
            embed.add_field(name=title[:256], value=preview[:1024], inline=False)
        if len(text_items) > len(text_blocks):
            embed.set_footer(text=f"仅展示前 {len(text_blocks)} 条文本内容")

    if owner_bypass:
        content = "👑 主人请拿好（原版内容）："
    else:
        content = (
            "✅ **获取成功！**\n"
            "🛡️ 文件附件仍包含溯源指纹，请勿私自转传。"
        )
        if text_items:
            content += "\n📝 纯文本内容已直接在下方显示。"

        today_start_iso = (
            datetime.now(TZ_SHANGHAI)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .isoformat()
        )
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM download_log WHERE user_id = ? AND timestamp >= ?",
                (interaction.user.id, today_start_iso),
            )
            cnt = (await cursor.fetchone())[0]
        content += f"\n今日剩余额度: {DAILY_DOWNLOAD_LIMIT - cnt}/{DAILY_DOWNLOAD_LIMIT}"

    send_kwargs = {
        "content": content,
        "embed": embed,
        "ephemeral": True,
    }
    if file_results:
        send_kwargs["files"] = make_discord_files_common(file_results)

    sent_message = await interaction.followup.send(**send_kwargs, wait=True)

    image_archive_notice = None
    image_archive_file = None
    if image_archive_pairs:
        archive_stem = _sanitize_virtual_filename(row["title"] or "images")
        image_archive_file = build_image_archive_file(
            image_archive_pairs,
            f"{archive_stem}_图片包.zip",
        )
        image_archive_notice = "可直接下载并解压，图片保留原始文件名。"

    image_link_items = []
    sent_attachments = list(getattr(sent_message, "attachments", []))
    for file_item, sent_attachment in zip(file_items, sent_attachments):
        sent_filename = getattr(sent_attachment, "filename", None)
        if not is_image_filename(sent_filename):
            continue
        image_link_items.append(
            {
                "label": file_item.get("original_filename")
                or file_item.get("filename")
                or sent_filename,
                "url": getattr(sent_attachment, "url", None),
            }
        )

    if not image_link_items:
        return

    result_embed = embed.copy() if embed else discord.Embed(
        title="📎 下载指引",
        color=discord.Color.teal(),
    )
    if image_link_items:
        result_embed.add_field(
            name="下载方式1：手动原图浏览器打开",
            value="点开上方图片打开大图→右上角【浏览器打开】→长按/右键浏览器原图另存为png，图片保留原始文件名。",
            inline=False,
        )
    add_image_download_fields(result_embed, image_link_items)
    if image_link_items:
        result_embed.add_field(
            name="\u200b",
            value="点击图片文件名可在浏览器中打开原图并另存为，但本方式文件名会丢失中文字符",
            inline=False,
        )
    if image_archive_notice:
        result_embed.add_field(
            name="下载方式3：图片文件打包",
            value=image_archive_notice,
            inline=False,
        )

    try:
        await sent_message.edit(embed=result_embed)
    except discord.HTTPException:
        pass

    if image_archive_file:
        try:
            await interaction.followup.send(
                content="压缩包",
                file=image_archive_file,
                ephemeral=True,
            )
        except discord.HTTPException:
            pass


async def check_rate_limit_and_report(
    interaction: discord.Interaction, bot, row
) -> tuple[bool, str]:
    """检查下载速率，先对用户温和提醒，达到日阈值后再上报。"""
    since_dt = datetime.now(TZ_SHANGHAI) - timedelta(
        minutes=DOWNLOAD_RATE_LIMIT_WINDOW_MINUTES
    )
    since_iso = since_dt.isoformat()
    logs = await protection_db.get_user_downloads_since(interaction.user.id, since_iso)

    if len(logs) < DOWNLOAD_RATE_LIMIT_MAX_TIMES:
        return True, ""

    now = datetime.now(TZ_SHANGHAI)
    today_start_iso = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    report_title = row["title"] if "title" in row.keys() and row["title"] else "未知标题"

    await protection_db.record_download_rate_warning(
        user_id=interaction.user.id,
        message_id=row["message_id"] if "message_id" in row.keys() else None,
        title=report_title,
        timestamp=now.isoformat(),
    )
    warning_count_today = await protection_db.count_user_download_rate_warnings_since(
        interaction.user.id, today_start_iso
    )

    should_report = (
        warning_count_today == DOWNLOAD_RATE_WARNING_DAILY_REPORT_THRESHOLD
    )
    if should_report:
        report_channel = bot.get_channel(ABNORMAL_REPORT_CHANNEL_ID)
        if not report_channel:
            try:
                report_channel = await bot.fetch_channel(ABNORMAL_REPORT_CHANNEL_ID)
            except Exception:
                report_channel = None

        if report_channel:
            recent_history = await protection_db.get_recent_download_history(
                interaction.user.id, limit=10
            )
            history_lines = []
            for item in recent_history[:8]:
                try:
                    ts = discord.utils.format_dt(datetime.fromisoformat(item["timestamp"]), "T")
                except Exception:
                    ts = item["timestamp"]
                channel_text = f"<#{item['channel_id']}>" if item["channel_id"] else "未知频道"
                history_lines.append(
                    f"- {ts} | {channel_text} | {item['title'] or '无标题'}"
                )

            embed = discord.Embed(
                title="🚨 异常下载速率告警",
                color=discord.Color.red(),
                description=(
                    f"用户: <@{interaction.user.id}> (`{interaction.user.id}`)\n"
                    f"短时阈值: {DOWNLOAD_RATE_LIMIT_WINDOW_MINUTES} 分钟内最多 {DOWNLOAD_RATE_LIMIT_MAX_TIMES} 次\n"
                    f"当前窗口命中: {len(logs)} 次\n"
                    f"当日警告次数: {warning_count_today}/{DOWNLOAD_RATE_WARNING_DAILY_REPORT_THRESHOLD}\n"
                    f"尝试下载: {report_title}"
                ),
                timestamp=now,
            )
            if history_lines:
                embed.add_field(
                    name="最近操作历史",
                    value="\n".join(history_lines)[:1024],
                    inline=False,
                )

            try:
                await report_channel.send(embed=embed)
            except Exception:
                pass

    return (
        False,
        (
            f"⏱️ 你刚刚下载得有点快啦，先稍微休息一下再继续吧。\n"
            f"为了照顾大家的使用体验，当前限制为 {DOWNLOAD_RATE_LIMIT_WINDOW_MINUTES} 分钟内最多 {DOWNLOAD_RATE_LIMIT_MAX_TIMES} 次下载。"
        ),
    )


# --- 延迟下载视图 (核心修改区域) ---
class AuthorNoteView(ui.View):
    def __init__(self, bot, row):
        super().__init__(timeout=300)
        self.bot = bot
        self.row = row
        self.downloaded = False

    @ui.button(
        label="⏳ 请阅读说明 (5s)", style=discord.ButtonStyle.secondary, disabled=True
    )
    async def btn_confirm(self, interaction: discord.Interaction, button: ui.Button):

        if self.downloaded:
            return
        self.downloaded = True

        # 禁用按钮防止重复点击，并更新提示
        button.disabled = True
        button.label = "🔍 正在处理溯源..."
        await interaction.response.edit_message(view=self)

        # 下载逻辑
        try:
            # 记录普通下载日志
            await record_download_common(interaction.user, self.row)
            await deliver_protected_content(interaction, self.bot, self.row)
        except Exception as e:
            import traceback

            traceback.print_exc()
            await interaction.followup.send(f"❌ 发生未知错误: {e}", ephemeral=True)


async def start_download_flow(interaction: discord.Interaction, bot, row):
    """
    通用下载流程控制：
    1. 显示作者提示 Embed
    2. 等待 5 秒
    3. 启用按钮供用户点击下载
    """
    author_note = row["log"] if row["log"] else "（作者未留下额外说明）"

    embed = discord.Embed(title="📝 作者提示", description=author_note, color=0xFFD700)
    embed.set_footer(text="请仔细阅读以上内容，5秒后可点击下方按钮获取文件。")

    view = AuthorNoteView(bot, row)

    # 发送消息（按钮初始是禁用的）
    if interaction.response.is_done():
        msg = await interaction.followup.send(embed=embed, view=view, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        msg = await interaction.original_response()

    # 延时 5 秒
    await asyncio.sleep(5)

    # 更新按钮状态
    view.children[0].disabled = False
    view.children[0].label = "✅ 我已阅读，获取附件"
    view.children[0].style = discord.ButtonStyle.success
    try:
        await msg.edit(view=view)
    except:
        pass  # 用户可能已经关闭了弹窗


# --- 自定义文件名选择视图 ---
class FileSelectView(ui.View):
    def __init__(self, protection_view):
        super().__init__(timeout=60)
        self.protection_view = protection_view
        options = []
        for i, entry in enumerate(protection_view.file_entries):
            current_name = entry["filename"]
            label = current_name[:95]
            description = (
                f"原始: {entry['original_filename'][:40]}{format_tag_text(entry)[:20]}"
            )
            options.append(
                discord.SelectOption(
                    label=f"{i + 1}. {label}",
                    value=str(i),
                    description=description[:100],
                )
            )
        self.select_menu = ui.Select(
            placeholder="选择要改名的文件...",
            options=options,
            min_values=1,
            max_values=1,
        )
        self.select_menu.callback = self.select_callback
        self.add_item(self.select_menu)

    async def select_callback(self, interaction: discord.Interaction):
        idx = int(self.select_menu.values[0])
        current_name = self.protection_view.file_entries[idx]["filename"]
        await interaction.response.send_modal(
            RenameFileModal(self.protection_view, idx, current_name)
        )


class DraftManageFilesSelectView(ui.View):
    def __init__(self, protection_view):
        super().__init__(timeout=180)
        self.protection_view = protection_view
        self.selected_index = 0
        options = []
        for i, entry in enumerate(self.protection_view.file_entries):
            options.append(
                discord.SelectOption(
                    label=f"{i + 1}. {entry['filename'][:90]}",
                    value=str(i),
                    description=(entry.get("original_filename") or entry["filename"])[:100],
                    default=(i == 0),
                )
            )
        self.select = ui.Select(placeholder="选择要操作的文件...", options=options)
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def on_select(self, interaction: discord.Interaction):
        self.selected_index = int(self.select.values[0])
        for idx, option in enumerate(self.select.options):
            option.default = idx == self.selected_index
        entry = self.protection_view.file_entries[self.selected_index]
        await interaction.response.edit_message(
            content=f"当前选中：{build_file_line(entry, self.selected_index + 1)}",
            view=self,
        )

    @ui.button(label="改名", style=discord.ButtonStyle.secondary, row=1, emoji="✏️")
    async def rename_file(self, interaction: discord.Interaction, button: ui.Button):
        current_name = self.protection_view.file_entries[self.selected_index]["filename"]
        await interaction.response.send_modal(
            RenameFileModal(self.protection_view, self.selected_index, current_name)
        )

    @ui.button(label="替换文件", style=discord.ButtonStyle.primary, row=1, emoji="♻️")
    async def replace_file(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message(
            "请在当前频道 5 分钟内发送 1 条带附件或纯文本的消息（附件会取第一个，纯文本会转成 .txt 进行替换）。",
            ephemeral=True,
        )

        def check(msg: discord.Message):
            return (
                msg.author.id == self.protection_view.user.id
                and msg.channel.id == interaction.channel.id
                and (len(msg.attachments) > 0 or has_collectible_message_content(msg))
            )

        try:
            msg = await self.protection_view.bot.wait_for("message", timeout=300, check=check)
        except asyncio.TimeoutError:
            return await interaction.followup.send(
                "⏰ 超时未收到可替换内容，已取消替换。",
                ephemeral=True,
            )

        old_entry = ensure_file_entry_defaults(
            self.protection_view.file_entries[self.selected_index]
        )
        try:
            updated_entry = await build_storage_entry_from_message(
                self.protection_view.bot,
                interaction.user,
                f"草稿替换: {self.protection_view.draft_title}",
                msg,
                old_tag=old_entry.get("tag"),
            )
        except Exception as exc:
            return await interaction.followup.send(
                f"❌ 替换文件失败：{exc}",
                ephemeral=True,
            )

        self.protection_view.attachments[self.selected_index] = ReusableStoredAttachment(
            updated_entry
        )
        self.protection_view.file_entries[self.selected_index] = ensure_file_entry_defaults(
            updated_entry
        )
        self.protection_view.sync_custom_names_from_entries()

        try:
            await msg.delete()
        except Exception:
            pass

        for idx, option in enumerate(self.select.options):
            name = self.protection_view.file_entries[idx].get("filename", "unknown")
            option.label = f"{idx + 1}. {name[:90]}"
            option.description = (
                self.protection_view.file_entries[idx].get("original_filename") or name
            )[:100]
            option.default = idx == self.selected_index

        try:
            await interaction.message.edit(
                content=f"当前选中：{build_file_line(self.protection_view.file_entries[self.selected_index], self.selected_index + 1)}",
                view=self,
            )
        except Exception:
            pass

        await self.protection_view.refresh_dashboard_message()
        await interaction.followup.send(
            f"✅ 已替换为：{self.protection_view.file_entries[self.selected_index]['filename']}",
            ephemeral=True,
        )

    @ui.button(label="删除文件", style=discord.ButtonStyle.danger, row=1, emoji="🗑️")
    async def delete_file(self, interaction: discord.Interaction, button: ui.Button):
        if len(self.protection_view.file_entries) <= 1:
            return await interaction.response.send_message(
                "❌ 当前仅剩 1 个文件，无法单独删除。",
                ephemeral=True,
            )

        removed = self.protection_view.file_entries.pop(self.selected_index)
        self.protection_view.attachments.pop(self.selected_index)
        if self.selected_index >= len(self.protection_view.file_entries):
            self.selected_index = len(self.protection_view.file_entries) - 1
        self.protection_view.sync_custom_names_from_entries()

        self.select.options = []
        for i, entry in enumerate(self.protection_view.file_entries):
            self.select.options.append(
                discord.SelectOption(
                    label=f"{i + 1}. {entry['filename'][:90]}",
                    value=str(i),
                    description=(entry.get("original_filename") or entry["filename"])[:100],
                    default=(i == self.selected_index),
                )
            )

        await self.protection_view.refresh_dashboard_message()
        await interaction.response.edit_message(
            content=(
                f"✅ 已删除：{removed.get('filename', 'unknown')}\n"
                f"当前选中：{build_file_line(self.protection_view.file_entries[self.selected_index], self.selected_index + 1)}"
            ),
            view=self,
        )


class DraftBatchFileView(ui.View):
    def __init__(self, protection_view, page_index=0):
        super().__init__(timeout=120)
        self.protection_view = protection_view
        self.page_index = page_index
        self.total_pages = max(
            1,
            (len(self.protection_view.file_entries) + FILE_EDIT_PAGE_SIZE - 1)
            // FILE_EDIT_PAGE_SIZE,
        )
        self._update_button_state()

    def _update_button_state(self):
        self.btn_prev.disabled = self.page_index <= 0
        self.btn_next.disabled = self.page_index >= self.total_pages - 1
        self.btn_open.label = f"批量编辑 第{self.page_index + 1}/{self.total_pages}页"

    def build_preview(self):
        start, end, entries = chunk_file_entries(
            self.protection_view.file_entries, self.page_index
        )
        lines = ["每行格式: 序号. 文件名", "标签请使用单独的标签面板设置。", ""]
        for idx, entry in enumerate(entries, start=start + 1):
            lines.append(build_file_line(entry, idx))
        preview = "\n".join(lines)
        return start, end, preview[:1900]

    @ui.button(label="上一页", style=discord.ButtonStyle.secondary)
    async def btn_prev(self, interaction: discord.Interaction, button: ui.Button):
        self.page_index -= 1
        self._update_button_state()
        _, _, preview = self.build_preview()
        await interaction.response.edit_message(content=preview, view=self)

    @ui.button(label="批量编辑", style=discord.ButtonStyle.primary)
    async def btn_open(self, interaction: discord.Interaction, button: ui.Button):
        start, end, _ = self.build_preview()
        modal = DraftBulkFilePageModal(self.protection_view, start, end)
        await interaction.response.send_modal(modal)

    @ui.button(label="下一页", style=discord.ButtonStyle.secondary)
    async def btn_next(self, interaction: discord.Interaction, button: ui.Button):
        self.page_index += 1
        self._update_button_state()
        _, _, preview = self.build_preview()
        await interaction.response.edit_message(content=preview, view=self)


class DraftBulkFilePageModal(ui.Modal, title="批量改文件名"):
    entries_input = ui.TextInput(
        label="每行: 序号. 文件名",
        style=discord.TextStyle.paragraph,
        max_length=4000,
    )

    def __init__(self, protection_view, start, end):
        super().__init__()
        self.protection_view = protection_view
        self.start = start
        self.end = end
        default_lines = []
        for idx, entry in enumerate(
            self.protection_view.file_entries[start:end], start=start + 1
        ):
            default_lines.append(f"{idx}. {entry['filename']}")
        self.entries_input.default = "\n".join(default_lines)[:4000]

    async def on_submit(self, interaction: discord.Interaction):
        try:
            updated = apply_rename_lines_to_entries(
                self.protection_view.file_entries,
                self.entries_input.value,
                self.start,
                self.end,
            )
        except ValueError as exc:
            return await interaction.response.send_message(f"❌ {exc}", ephemeral=True)

        self.protection_view.sync_custom_names_from_entries()
        await interaction.response.defer(ephemeral=True)
        await self.protection_view.update_dashboard(interaction)
        await interaction.followup.send(
            f"✅ 已更新当前页 {updated} 个文件名。", ephemeral=True
        )


class PublishedBatchFileView(ui.View):
    def __init__(self, message_id, file_data, page_index=0):
        super().__init__(timeout=120)
        self.message_id = message_id
        self.file_data = file_data
        self.page_index = page_index
        self.total_pages = max(
            1, (len(self.file_data) + FILE_EDIT_PAGE_SIZE - 1) // FILE_EDIT_PAGE_SIZE
        )
        self._update_button_state()

    def _update_button_state(self):
        self.btn_prev.disabled = self.page_index <= 0
        self.btn_next.disabled = self.page_index >= self.total_pages - 1
        self.btn_open.label = f"批量编辑 第{self.page_index + 1}/{self.total_pages}页"

    def build_preview(self):
        start, end, entries = chunk_file_entries(self.file_data, self.page_index)
        lines = ["每行格式: 序号. 文件名", "标签请用下方按钮选择。", ""]
        for idx, entry in enumerate(entries, start=start + 1):
            lines.append(build_file_line(entry, idx))
        return start, end, "\n".join(lines)[:1900]

    @ui.button(label="上一页", style=discord.ButtonStyle.secondary)
    async def btn_prev(self, interaction: discord.Interaction, button: ui.Button):
        self.page_index -= 1
        self._update_button_state()
        _, _, preview = self.build_preview()
        await interaction.response.edit_message(content=preview, view=self)

    @ui.button(label="批量编辑", style=discord.ButtonStyle.primary)
    async def btn_open(self, interaction: discord.Interaction, button: ui.Button):
        start, end, _ = self.build_preview()
        await interaction.response.send_modal(
            PublishedBulkFilePageModal(self.message_id, self.file_data, start, end)
        )

    @ui.button(label="下一页", style=discord.ButtonStyle.secondary)
    async def btn_next(self, interaction: discord.Interaction, button: ui.Button):
        self.page_index += 1
        self._update_button_state()
        _, _, preview = self.build_preview()
        await interaction.response.edit_message(content=preview, view=self)


class PublishedBulkFilePageModal(ui.Modal, title="批量改已发布文件名"):
    entries_input = ui.TextInput(
        label="每行: 序号. 文件名",
        style=discord.TextStyle.paragraph,
        max_length=4000,
    )

    def __init__(self, message_id, file_data, start, end):
        super().__init__()
        self.message_id = message_id
        self.file_data = file_data
        self.start = start
        self.end = end
        default_lines = []
        for idx, entry in enumerate(self.file_data[start:end], start=start + 1):
            default_lines.append(f"{idx}. {entry['filename']}")
        self.entries_input.default = "\n".join(default_lines)[:4000]

    async def on_submit(self, interaction: discord.Interaction):
        try:
            updated = apply_rename_lines_to_entries(
                self.file_data,
                self.entries_input.value,
                self.start,
                self.end,
            )
        except ValueError as exc:
            return await interaction.response.send_message(f"❌ {exc}", ephemeral=True)

        async with get_db() as db:
            await db.execute(
                "UPDATE protected_items SET storage_urls = ? WHERE message_id = ?",
                (json.dumps(self.file_data), self.message_id),
            )
            await db.commit()

        await refresh_protected_post_embed(interaction.client, self.message_id)

        await interaction.response.send_message(
            f"✅ 已更新当前页 {updated} 个已发布文件名。", ephemeral=True
        )


class FileTargetSelect(ui.Select):
    def __init__(self, owner_view):
        self.owner_view = owner_view
        start, end, entries = chunk_file_entries(
            owner_view.get_entries(), owner_view.page_index
        )
        options = []
        for idx, entry in enumerate(entries, start=start):
            options.append(
                discord.SelectOption(
                    label=build_file_line(entry, idx + 1)[:100],
                    value=str(idx),
                    default=idx == owner_view.selected_file_index,
                )
            )
        super().__init__(
            placeholder="选择要设置标签的文件",
            options=options,
            min_values=1,
            max_values=1,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        self.owner_view.selected_file_index = int(self.values[0])
        self.owner_view.refresh_components()
        _, _, preview = self.owner_view.build_preview()
        await interaction.response.edit_message(content=preview, view=self.owner_view)


class FileTagSelect(ui.Select):
    def __init__(self, owner_view):
        self.owner_view = owner_view
        file_index = owner_view.selected_file_index
        entry = owner_view.get_entry(file_index)
        current_tag = entry.get("tag")
        options = [
            discord.SelectOption(
                label="无标签", value="__none__", default=current_tag is None
            )
        ]
        for tag in FILE_TAG_OPTIONS:
            options.append(
                discord.SelectOption(label=tag, value=tag, default=current_tag == tag)
            )
        super().__init__(
            placeholder=f"为文件 {file_index + 1} 选择标签",
            options=options,
            min_values=1,
            max_values=1,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        selected = self.values[0]
        tag_value = None if selected == "__none__" else selected
        await self.owner_view.apply_tag(
            interaction, self.owner_view.selected_file_index, tag_value
        )


class BaseFileTagView(ui.View):
    def __init__(self, page_index=0, timeout=120):
        super().__init__(timeout=timeout)
        self.page_index = page_index
        self.selected_file_index = 0

    def get_entries(self):
        raise NotImplementedError

    def get_entry(self, index):
        return self.get_entries()[index]

    async def persist(self):
        return None

    async def apply_tag(self, interaction, file_index, tag_value):
        entry = self.get_entry(file_index)
        entry["tag"] = tag_value
        await self.persist()
        self.refresh_components()
        _, _, preview = self.build_preview()
        await interaction.response.edit_message(content=preview, view=self)

    def refresh_components(self):
        self.clear_items()
        self._update_button_state()
        start, end, _ = chunk_file_entries(self.get_entries(), self.page_index)
        if self.selected_file_index < start or self.selected_file_index >= end:
            self.selected_file_index = start
        self.add_item(FileTargetSelect(self))
        self.add_item(FileTagSelect(self))
        self.add_item(self.btn_prev)
        self.add_item(self.btn_next)

    def _update_button_state(self):
        self.btn_prev.disabled = self.page_index <= 0
        self.btn_next.disabled = self.page_index >= self.total_pages - 1

    def build_preview(self):
        start, end, entries = chunk_file_entries(self.get_entries(), self.page_index)
        lines = ["请选择文件后再设置标签：", ""]
        for idx, entry in enumerate(entries, start=start + 1):
            prefix = "👉 " if idx - 1 == self.selected_file_index else "   "
            lines.append(f"{prefix}{build_file_line(entry, idx)}")
        return start, end, "\n".join(lines)[:1900]

    @ui.button(label="上一页", style=discord.ButtonStyle.secondary)
    async def btn_prev(self, interaction: discord.Interaction, button: ui.Button):
        self.page_index -= 1
        self.refresh_components()
        _, _, preview = self.build_preview()
        await interaction.response.edit_message(content=preview, view=self)

    @ui.button(label="下一页", style=discord.ButtonStyle.secondary)
    async def btn_next(self, interaction: discord.Interaction, button: ui.Button):
        self.page_index += 1
        self.refresh_components()
        _, _, preview = self.build_preview()
        await interaction.response.edit_message(content=preview, view=self)


class DraftTagBatchView(BaseFileTagView):
    def __init__(self, protection_view, page_index=0):
        self.protection_view = protection_view
        self.total_pages = max(
            1,
            (len(self.protection_view.file_entries) + FILE_EDIT_PAGE_SIZE - 1)
            // FILE_EDIT_PAGE_SIZE,
        )
        super().__init__(page_index=page_index, timeout=120)
        self.refresh_components()

    def get_entries(self):
        return self.protection_view.file_entries

    async def persist(self):
        self.protection_view.sync_custom_names_from_entries()


class PublishedTagBatchView(BaseFileTagView):
    def __init__(self, message_id, file_data, page_index=0):
        self.message_id = message_id
        self.file_data = file_data
        self.total_pages = max(
            1,
            (len(self.file_data) + FILE_EDIT_PAGE_SIZE - 1) // FILE_EDIT_PAGE_SIZE,
        )
        super().__init__(page_index=page_index, timeout=120)
        self.refresh_components()

    def get_entries(self):
        return self.file_data

    async def persist(self):
        async with get_db() as db:
            await db.execute(
                "UPDATE protected_items SET storage_urls = ? WHERE message_id = ?",
                (json.dumps(self.file_data), self.message_id),
            )
            await db.commit()


class UploadSessionControlView(ui.View):
    def __init__(self, cog, user_id, channel_id):
        super().__init__(timeout=UPLOAD_SESSION_TIMEOUT_SECONDS)
        self.cog = cog
        self.user_id = user_id
        self.channel_id = channel_id

    def _build_embed(self):
        session = self.cog.get_upload_session(self.user_id, self.channel_id)
        if not session:
            return discord.Embed(
                title="📥 附件收集中",
                description="当前收集会话已结束或已失效。",
                color=discord.Color.red(),
            )

        message_attachment_count = sum(
            count_collectible_message_items(msg) for msg in session["messages"]
        )
        reused_attachment_count = len(session.get("reusable_attachments", []))
        attachment_count = message_attachment_count + reused_attachment_count
        msg_count = len(session["messages"])
        expire_text = discord.utils.format_dt(session["expires_at"], "R")
        title = session.get("panel_title") or "📥 保护附件收集中"
        description = session.get("panel_description") or (
            "接下来 5 分钟内，你在当前频道发送的带附件或纯文本消息都会加入本次保护附件草稿。\n"
            "推荐做法：直接正常发送文件、链接或文本内容，全部发完后点下方 `上传完毕`。"
        )
        note_text = session.get("panel_note") or (
            "此流程绕开 slash 附件参数，既支持保留原始文件名，也支持把纯文本内容转成可保护的文本附件。"
        )
        embed = discord.Embed(
            title=title,
            color=0x87CEEB,
            description=description,
        )
        embed.add_field(name="已收集消息", value=str(msg_count), inline=True)
        embed.add_field(name="已收集项目", value=str(attachment_count), inline=True)
        embed.add_field(name="截止时间", value=expire_text, inline=True)
        if reused_attachment_count > 0:
            embed.add_field(
                name="复用已发布",
                value=f"已导入 {reused_attachment_count} 个",
                inline=True,
            )
        embed.add_field(
            name="说明",
            value=note_text,
            inline=False,
        )
        return embed

    @ui.button(label="刷新状态", style=discord.ButtonStyle.secondary, emoji="🔄")
    async def refresh_status(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.edit_message(
            embed=self._build_embed(),
            view=self,
        )

    @ui.button(label="复用已发布", style=discord.ButtonStyle.secondary, emoji="♻️")
    async def reuse_published(self, interaction: discord.Interaction, button: ui.Button):
        rows = await get_user_published_items(self.user_id)
        if not rows:
            return await interaction.response.send_message(
                "❌ 你的附件库里还没有已发布内容可复用。",
                ephemeral=True,
            )
        await interaction.response.send_message(
            "请选择要导入到当前收集会话的已发布附件批次：",
            view=UploadSessionReusePublishedView(self, rows),
            ephemeral=True,
        )

    @ui.button(label="上传完毕", style=discord.ButtonStyle.success, emoji="✅")
    async def finish_upload(self, interaction: discord.Interaction, button: ui.Button):
        session = self.cog.get_upload_session(self.user_id, self.channel_id)
        finish_handler = session.get("finish_handler") if session else None
        if finish_handler:
            await finish_handler(interaction)
            return
        await self.cog.finish_upload_session(interaction, self.user_id, self.channel_id)

    @ui.button(label="取消上传", style=discord.ButtonStyle.danger, emoji="✖️")
    async def cancel_upload(self, interaction: discord.Interaction, button: ui.Button):
        session = self.cog.get_upload_session(self.user_id, self.channel_id) or {}
        await self.cog.cancel_upload_session(
            interaction,
            self.user_id,
            self.channel_id,
            reason=session.get("cancel_reason") or "已取消本次附件收集。",
        )


# --- 草稿/发布视图 ---
class UploadSessionReusePublishedView(ui.View):
    def __init__(self, session_view, posts_rows):
        super().__init__(timeout=120)
        self.session_view = session_view
        self.posts_rows = posts_rows
        self.selected_message_id = str(posts_rows[0]["message_id"])
        options = []
        for idx, row in enumerate(posts_rows[:25]):
            try:
                file_count = len(
                    [item for item in json.loads(row["storage_urls"] or "[]") if isinstance(item, dict)]
                )
            except Exception:
                file_count = 0
            try:
                ts_str = datetime.fromisoformat(row["created_at"]).strftime("%m-%d %H:%M")
            except Exception:
                ts_str = "未知时间"
            options.append(
                discord.SelectOption(
                    label=(row["title"] or "无标题")[:90],
                    value=str(row["message_id"]),
                    description=f"{file_count} 项 | {ts_str} | 频道 {row['channel_id']}",
                    default=(idx == 0),
                )
            )
        self.select_menu = ui.Select(
            placeholder="选择要复用的已发布附件批次...",
            options=options,
            row=0,
        )
        self.select_menu.callback = self.on_select
        self.add_item(self.select_menu)

    async def on_select(self, interaction: discord.Interaction):
        self.selected_message_id = self.select_menu.values[0]
        for option in self.select_menu.options:
            option.default = option.value == self.selected_message_id
        await interaction.response.edit_message(view=self)

    @ui.button(label="导入整批附件", style=discord.ButtonStyle.success, row=1, emoji="📥")
    async def import_post(self, interaction: discord.Interaction, button: ui.Button):
        row, reusable_attachments = get_reusable_attachments_from_rows(
            self.posts_rows,
            self.selected_message_id,
        )
        if not row:
            return await interaction.response.send_message(
                "❌ 未找到对应的附件批次，请重试。",
                ephemeral=True,
            )
        if not reusable_attachments:
            return await interaction.response.send_message(
                "❌ 这条已发布记录里没有可复用的附件。",
                ephemeral=True,
            )

        session = self.session_view.cog.get_upload_session(
            self.session_view.user_id,
            self.session_view.channel_id,
        )
        if not session:
            return await interaction.response.send_message(
                "❌ 当前收集会话已失效，请重新开始。",
                ephemeral=True,
            )

        session.setdefault("reusable_attachments", []).extend(reusable_attachments)
        session["reusable_metadata"] = build_reusable_metadata_from_row(row)
        await interaction.response.defer(ephemeral=True)
        try:
            await interaction.edit_original_response(
                embed=self.session_view._build_embed(),
                view=self.session_view,
            )
        except Exception:
            pass
        await interaction.followup.send(
            f"✅ 已导入 **{len(reusable_attachments)}** 个已发布附件到当前收集会话，并同步了标题、作者提示、更新日志与获取条件配置。",
            ephemeral=True,
        )


class DraftReusePublishedView(ui.View):
    def __init__(self, protection_view, posts_rows):
        super().__init__(timeout=120)
        self.protection_view = protection_view
        self.posts_rows = posts_rows
        self.selected_message_id = str(posts_rows[0]["message_id"])
        options = []
        for idx, row in enumerate(posts_rows[:25]):
            try:
                file_count = len(
                    [item for item in json.loads(row["storage_urls"] or "[]") if isinstance(item, dict)]
                )
            except Exception:
                file_count = 0
            try:
                ts_str = datetime.fromisoformat(row["created_at"]).strftime("%m-%d %H:%M")
            except Exception:
                ts_str = "未知时间"
            options.append(
                discord.SelectOption(
                    label=(row["title"] or "无标题")[:90],
                    value=str(row["message_id"]),
                    description=f"{file_count} 项 | {ts_str} | 频道 {row['channel_id']}",
                    default=(idx == 0),
                )
            )
        self.select_menu = ui.Select(
            placeholder="选择要复用的已发布附件批次...",
            options=options,
            row=0,
        )
        self.select_menu.callback = self.on_select
        self.add_item(self.select_menu)

    async def on_select(self, interaction: discord.Interaction):
        self.selected_message_id = self.select_menu.values[0]
        for option in self.select_menu.options:
            option.default = option.value == self.selected_message_id
        await interaction.response.edit_message(view=self)

    @ui.button(label="导入整批附件", style=discord.ButtonStyle.success, row=1, emoji="📥")
    async def import_post(self, interaction: discord.Interaction, button: ui.Button):
        row, reusable_attachments = get_reusable_attachments_from_rows(
            self.posts_rows,
            self.selected_message_id,
        )
        if not row:
            return await interaction.response.send_message(
                "❌ 未找到对应的附件批次，请重试。",
                ephemeral=True,
            )
        if not reusable_attachments:
            return await interaction.response.send_message(
                "❌ 这条已发布记录里没有可复用的附件。",
                ephemeral=True,
            )

        self.protection_view.append_attachments(reusable_attachments)
        self.protection_view.apply_draft_defaults(build_reusable_metadata_from_row(row))
        await interaction.response.defer(ephemeral=True)
        await self.protection_view.refresh_dashboard_message()
        await interaction.followup.send(
            f"✅ 已导入 **{len(reusable_attachments)}** 个已发布附件到当前草稿，并同步了标题、作者提示、更新日志与获取条件配置。",
            ephemeral=True,
        )


class ProtectionDraftView(ui.View):
    def __init__(
        self,
        bot,
        user,
        attachments,
        target_message=None,
        default_log=None,
        draft_defaults=None,
    ):
        super().__init__(timeout=600)
        self.bot = bot
        self.user = user
        self.attachments = attachments
        self.target_message = target_message
        self.draft_title = f"{user.display_name} 的保护附件"
        self.draft_log = default_log
        self.draft_update_log = None
        self.mention_users = False
        self.draft_password = None
        self.draft_mode = "like"
        self.dashboard_message = None
        self.file_entries = []
        for att in attachments:
            self.file_entries.append(self._build_file_entry_for_attachment(att))
        self.custom_names = {
            idx: entry["filename"]
            for idx, entry in enumerate(self.file_entries)
            if entry["filename"] != entry["original_filename"]
        }
        self.apply_draft_defaults(draft_defaults)

    def apply_draft_defaults(self, draft_defaults):
        if not draft_defaults:
            return
        if draft_defaults.get("draft_title"):
            self.draft_title = draft_defaults["draft_title"]
        if "draft_log" in draft_defaults:
            self.draft_log = draft_defaults.get("draft_log")
        if "draft_update_log" in draft_defaults:
            self.draft_update_log = draft_defaults.get("draft_update_log")
        if draft_defaults.get("draft_mode"):
            self.draft_mode = draft_defaults["draft_mode"]
        if "mention_users" in draft_defaults:
            self.mention_users = bool(draft_defaults.get("mention_users"))
        if "draft_password" in draft_defaults:
            self.draft_password = draft_defaults.get("draft_password")

    def _build_file_entry_for_attachment(self, att):
        stored_entry = getattr(att, "stored_entry", None)
        if stored_entry:
            return ensure_file_entry_defaults(dict(stored_entry))

        original_name = get_attachment_original_name(att)
        return ensure_file_entry_defaults(
            {
                "filename": original_name,
                "original_filename": original_name,
                "tag": None,
            }
        )

    def append_attachments(self, attachments):
        for att in attachments:
            self.attachments.append(att)
            self.file_entries.append(self._build_file_entry_for_attachment(att))
        self.sync_custom_names_from_entries()

    async def start_append_session(self, interaction: discord.Interaction):
        cog = self.bot.get_cog("ProtectionCog")
        if not cog:
            return await interaction.response.send_message(
                "❌ 未找到 ProtectionCog，暂时无法启动追加会话。",
                ephemeral=True,
            )

        key = cog._session_key(interaction.user.id, interaction.channel.id)
        old_session = cog.upload_sessions.pop(key, None)
        if old_session and old_session.get("task"):
            old_session["task"].cancel()

        expires_at = datetime.now(TZ_SHANGHAI) + timedelta(
            seconds=UPLOAD_SESSION_TIMEOUT_SECONDS
        )
        task = self.bot.loop.create_task(
            cog._expire_upload_session(
                interaction.user.id,
                interaction.channel.id,
                expires_at,
            )
        )

        async def finish_append_upload(done_interaction: discord.Interaction):
            session = cog.upload_sessions.pop(key, None)
            if not session:
                return await done_interaction.response.edit_message(
                    content="❌ 当前没有可提交的附件收集会话。",
                    embed=None,
                    view=None,
                )

            if session.get("task"):
                session["task"].cancel()

            source_messages = session["messages"]
            reusable_attachments = list(session.get("reusable_attachments", []))
            try:
                attachments, _default_log = await collect_cached_attachments_from_messages(
                    source_messages
                )
            except Exception as exc:
                return await done_interaction.response.edit_message(
                    content=f"❌ 追加附件失败：{exc}",
                    embed=None,
                    view=None,
                )

            attachments.extend(reusable_attachments)
            if not attachments:
                return await done_interaction.response.edit_message(
                    content="❌ 还没有收集到任何可追加内容，请先发送带附件或纯文本的消息。",
                    embed=None,
                    view=None,
                )

            self.append_attachments(attachments)
            reusable_metadata = dict(session.get("reusable_metadata", {}) or {})
            if reusable_metadata:
                self.apply_draft_defaults(reusable_metadata)

            for msg in source_messages:
                try:
                    await msg.delete()
                except Exception:
                    pass

            await self.refresh_dashboard_message()
            await done_interaction.response.edit_message(
                content=f"✅ 已成功追加 **{len(attachments)}** 个附件到当前草稿。",
                embed=None,
                view=None,
            )

        cog.upload_sessions[key] = {
            "messages": [],
            "reusable_attachments": [],
            "reusable_metadata": {},
            "expires_at": expires_at,
            "task": task,
            "finish_handler": finish_append_upload,
            "cancel_reason": "已取消本次草稿附件追加。",
            "panel_title": "📥 草稿附件收集中",
            "panel_description": (
                "接下来 5 分钟内，你在当前频道发送的带附件或纯文本消息都会追加到当前草稿里。\n"
                "全部发完后点击下方 `上传完毕` 即可写入草稿。"
            ),
            "panel_note": "收集规则与发布附件一致：支持多条消息、多附件，纯文本会自动转成可保护的文本附件。",
        }

        view = UploadSessionControlView(cog, interaction.user.id, interaction.channel.id)
        await interaction.response.send_message(
            embed=view._build_embed(),
            view=view,
            ephemeral=True,
        )

    def sync_custom_names_from_entries(self):
        self.custom_names = {
            idx: entry["filename"]
            for idx, entry in enumerate(self.file_entries)
            if entry["filename"] != entry["original_filename"]
        }

    def count_tagged_files(self):
        return sum(1 for entry in self.file_entries if entry.get("tag"))

    def build_dashboard_embed(self):
        log_preview = (
            self.draft_log[:50] + "..."
            if self.draft_log and len(self.draft_log) > 50
            else self.draft_log
        )
        renamed_count = len(self.custom_names)
        file_status = f"{len(self.attachments)} 个"
        if renamed_count > 0:
            file_status += f" (已改名 {renamed_count} 个)"
        tagged_count = self.count_tagged_files()
        if tagged_count > 0:
            file_status += f" / 已标记 {tagged_count} 个"

        update_log_preview = (
            self.draft_update_log[:50] + "..."
            if self.draft_update_log and len(self.draft_update_log) > 50
            else self.draft_update_log
        )
        status_desc = (
            f"📦 **已传文件**: {file_status}\n"
            f"🏷️ **当前标题**: {self.draft_title}\n"
            f"📝 **作者提示**: {'✅ ' + log_preview if self.draft_log else '⚪ 未设置'}\n"
            f"🗒️ **更新日志**: {'✅ ' + update_log_preview if self.draft_update_log else '⚪ 未设置'}\n"
            f"📣 **艾特贴内用户**: {'✅ 开启' if self.mention_users else '⚪ 关闭'}\n"
        )
        mode_map = {
            "like": "👍 点赞解锁",
            "like_comment": "💬 点赞+评论",
            "like_password": f"🔐 点赞+口令 (口令: ||{self.draft_password}||)",
            "like_comment_password": f"🔐💬 点赞+评论+口令 (口令: ||{self.draft_password}||)",
        }
        status_desc += f"⚙️ **获取方式**: {mode_map.get(self.draft_mode)}"
        guide_desc = "1️⃣ 点击 **第一排** 修改标题、说明、更新日志，查看当前文件列表。\n2️⃣ 点击 **第二排** 进行单文件操作、批量编辑、设置标签、复用已发布或追加附件。\n3️⃣ 点击 **第三排** 选择解锁条件或配置 **艾特贴内用户**。\n4️⃣ 确认无误后点击 **🚀 确认发布**。"
        embed = discord.Embed(title="🛠️ 附件保护控制台", color=0x87CEEB)
        embed.add_field(name="📊 当前配置状态", value=status_desc, inline=False)
        embed.add_field(name="📖 操作指引", value=guide_desc, inline=False)
        embed.set_footer(text="此面板仅你自己可见")
        return embed

    async def refresh_dashboard_message(self):
        if not self.dashboard_message:
            return
        try:
            await self.dashboard_message.edit(
                content=None,
                embed=self.build_dashboard_embed(),
                view=self,
            )
        except Exception:
            pass

    async def update_dashboard(self, interaction: discord.Interaction):
        embed = self.build_dashboard_embed()
        if self.dashboard_message is None and interaction.message:
            self.dashboard_message = interaction.message

        if interaction.response.is_done():
            try:
                await interaction.edit_original_response(
                    content=None, embed=embed, view=self
                )
            except:
                await self.refresh_dashboard_message()
        else:
            await interaction.response.edit_message(
                content=None, embed=embed, view=self
            )
            self.dashboard_message = interaction.message

    @ui.button(label="修改标题", style=discord.ButtonStyle.secondary, row=0, emoji="🏷️")
    async def btn_set_title(self, i: discord.Interaction, b: ui.Button):
        await i.response.send_modal(DraftTitleModal(self))

    @ui.button(label="作者提示", style=discord.ButtonStyle.secondary, row=0, emoji="📝")
    async def btn_set_note(self, i: discord.Interaction, b: ui.Button):
        await i.response.send_modal(DraftNoteModal(self))

    @ui.button(label="更新日志", style=discord.ButtonStyle.secondary, row=0, emoji="🗒️")
    async def btn_set_update_log(self, i: discord.Interaction, b: ui.Button):
        await i.response.send_modal(DraftUpdateLogModal(self))

    @ui.button(label="单文件操作", style=discord.ButtonStyle.secondary, row=1, emoji="✏️")
    async def btn_rename_files(self, i: discord.Interaction, b: ui.Button):
        await i.response.send_message(
            "请选择文件并执行操作（改名/替换/删除）：",
            view=DraftManageFilesSelectView(self),
            ephemeral=True,
        )

    @ui.button(
        label="批量编辑文件", style=discord.ButtonStyle.secondary, row=1, emoji="🧩"
    )
    async def btn_batch_edit_files(self, i: discord.Interaction, b: ui.Button):
        view = DraftBatchFileView(self)
        _, _, preview = view.build_preview()
        await i.response.send_message(preview, view=view, ephemeral=True)

    @ui.button(label="设置标签", style=discord.ButtonStyle.secondary, row=1, emoji="🏷️")
    async def btn_set_tags(self, i: discord.Interaction, b: ui.Button):
        view = DraftTagBatchView(self)
        _, _, preview = view.build_preview()
        await i.response.send_message(preview, view=view, ephemeral=True)

    @ui.button(label="查看文件", style=discord.ButtonStyle.secondary, row=0, emoji="📦")
    async def btn_view_files(self, i: discord.Interaction, b: ui.Button):
        names = []
        for idx, entry in enumerate(self.file_entries):
            names.append(build_file_line(entry, idx + 1))
        await i.response.send_message(
            f"**当前文件列表：**\n" + "\n".join(names)[:1900], ephemeral=True
        )

    @ui.button(label="复用已发布", style=discord.ButtonStyle.secondary, row=1, emoji="♻️")
    async def btn_reuse_published(self, i: discord.Interaction, b: ui.Button):
        rows = await get_user_published_items(self.user.id)
        if not rows:
            return await i.response.send_message(
                "❌ 你的附件库里还没有已发布内容可复用。",
                ephemeral=True,
            )
        await i.response.send_message(
            "请选择要导入到当前草稿的已发布附件批次：",
            view=DraftReusePublishedView(self, rows),
            ephemeral=True,
        )

    @ui.button(label="追加附件", style=discord.ButtonStyle.primary, row=1, emoji="➕")
    async def btn_append_files(self, i: discord.Interaction, b: ui.Button):
        await self.start_append_session(i)

    @ui.button(label="点赞", style=discord.ButtonStyle.primary, row=2)
    async def mode_like(self, i: discord.Interaction, b: ui.Button):
        self.draft_mode = "like"
        await self.update_dashboard(i)

    @ui.button(label="点赞+评论", style=discord.ButtonStyle.primary, row=2)
    async def mode_like_comment(self, i: discord.Interaction, b: ui.Button):
        self.draft_mode = "like_comment"
        await self.update_dashboard(i)

    @ui.button(label="点赞+口令", style=discord.ButtonStyle.success, row=2, emoji="🔐")
    async def mode_like_pass(self, i: discord.Interaction, b: ui.Button):
        await i.response.send_modal(DraftPasswordModal(self, "like_password"))

    @ui.button(
        label="点赞+评论+口令", style=discord.ButtonStyle.success, row=2, emoji="🔐"
    )
    async def mode_like_comm_pass(self, i: discord.Interaction, b: ui.Button):
        await i.response.send_modal(DraftPasswordModal(self, "like_comment_password"))

    @ui.button(
        label="艾特贴内用户", style=discord.ButtonStyle.secondary, row=2, emoji="📣"
    )
    async def toggle_mention_users(self, i: discord.Interaction, b: ui.Button):
        self.mention_users = not self.mention_users
        await self.update_dashboard(i)

    @ui.button(label="确认发布", style=discord.ButtonStyle.danger, row=4, emoji="🚀")
    async def btn_confirm(self, i: discord.Interaction, b: ui.Button):
        await i.response.edit_message(
            content="⏳ 正在加密上传...", embed=None, view=None
        )
        await self.publish(i)

    @ui.button(label="取消", style=discord.ButtonStyle.gray, row=4, emoji="✖️")
    async def btn_cancel(self, i: discord.Interaction, b: ui.Button):
        await i.response.edit_message(content="操作已取消。", embed=None, view=None)
        self.stop()

    async def publish(self, interaction: discord.Interaction):
        try:
            stored_data = await build_storage_entries_from_attachments(
                self.bot,
                self.user,
                self.draft_title,
                self.attachments,
                self.file_entries,
            )
        except Exception as e:
            return await interaction.followup.send(f"文件读取失败：{e}", ephemeral=True)

        if self.target_message:
            try:
                await self.target_message.delete()
            except:
                pass

        now_ts = discord.utils.format_dt(datetime.now(TZ_SHANGHAI))
        embed = build_protected_post_embed(
            title=self.draft_title,
            unlock_type=self.draft_mode,
            file_count=len(stored_data),
            author_name=f"由 {self.user.display_name} 发布",
            author_icon_url=self.user.display_avatar.url,
            publish_time_text=now_ts,
            bot_avatar_url=self.bot.user.display_avatar.url,
        )

        final_msg = await interaction.channel.send(embed=embed)
        try:
            await final_msg.pin(reason="附件保护自动标注")
        except:
            await interaction.followup.send("提示：我没有置顶权限！", ephemeral=True)

        async with get_db() as db:
            await db.execute(
                """INSERT INTO protected_items (message_id, channel_id, owner_id, unlock_type, storage_urls, title, log, update_log, mention_users, password, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    final_msg.id,
                    final_msg.channel.id,
                    self.user.id,
                    self.draft_mode,
                    json.dumps(stored_data),
                    self.draft_title,
                    self.draft_log,
                    self.draft_update_log,
                    int(self.mention_users),
                    self.draft_password,
                    datetime.now(TZ_SHANGHAI).isoformat(),
                ),
            )
            await db.commit()

        # 自动开启置底（兼容手动开关）
        try:
            channel_id = interaction.channel.id
            if channel_id not in self.bot.get_cog("ProtectionCog").bump_tasks:
                task = self.bot.loop.create_task(
                    self.bot.get_cog("ProtectionCog")._bump_loop(interaction.channel)
                )
                self.bot.get_cog("ProtectionCog").bump_tasks[channel_id] = task
            await protection_db.add_bump_config(channel_id)
            await self.bot.get_cog("ProtectionCog")._execute_bump_once(
                interaction.channel
            )
        except Exception:
            pass

        # 发布更新通知（若配置）
        if self.mention_users or self.draft_update_log:
            mention_text = "@everyone" if self.mention_users else ""

            if mention_text:
                await interaction.channel.send(f"📣 **更新通知** {mention_text}")

            if self.draft_update_log:
                update_header = f"🗒️ **{self.draft_title} 更新日志**"
                update_msg = await interaction.channel.send(
                    f"{update_header}\n{self.draft_update_log}"
                )
                await protection_db.record_attachment_update_publish_log(
                    owner_id=self.user.id,
                    guild_id=getattr(interaction.guild, "id", None),
                    channel_id=interaction.channel.id,
                    protected_message_id=final_msg.id,
                    update_message_id=update_msg.id,
                    title=self.draft_title,
                    update_log=self.draft_update_log,
                    timestamp=datetime.now(TZ_SHANGHAI).isoformat(),
                    mention_users=self.mention_users,
                )
                try:
                    await update_msg.pin(reason="附件更新日志标注")
                except:
                    await interaction.followup.send(
                        "提示：我没有置顶权限，更新日志未能自动标注。", ephemeral=True
                    )

        await interaction.followup.send(
            "✅ 发布成功！已移除直接获取按钮，引导用户使用命令。", ephemeral=True
        )


# --- 帖子列表视图 ---
class EditPublishedFileModal(ui.Modal, title="修改已发布文件名"):
    name_input = ui.TextInput(
        label="新文件名 (无需输入后缀)", placeholder="请输入新名字", max_length=100
    )

    def __init__(self, message_id, file_index, file_data):
        super().__init__()
        self.message_id = message_id
        self.file_index = file_index
        self.file_data = file_data
        ensure_file_entry_defaults(self.file_data[file_index])
        current_name = file_data[file_index].get("filename", "unknown.ext")
        self.name_stem, self.ext = os.path.splitext(current_name)
        self.name_input.default = self.name_stem

    async def on_submit(self, interaction: discord.Interaction):
        new_stem = self.name_input.value.strip()
        if not new_stem:
            return await interaction.response.send_message(
                "文件名不能为空！", ephemeral=True
            )
        new_full_name = f"{new_stem}{self.ext}"

        # 更新内存数据
        self.file_data[self.file_index]["filename"] = new_full_name

        # 更新数据库
        async with get_db() as db:
            await db.execute(
                "UPDATE protected_items SET storage_urls = ? WHERE message_id = ?",
                (json.dumps(self.file_data), self.message_id),
            )
            await db.commit()

        await refresh_protected_post_embed(interaction.client, self.message_id)

        await interaction.response.send_message(
            f"✅ 修改成功！文件已更名为 `{new_full_name}`", ephemeral=True
        )


class EditProtectionTitleModal(ui.Modal, title="修改附件标题"):
    title_input = ui.TextInput(
        label="新标题", placeholder="请输入新的显示标题", max_length=100
    )

    def __init__(self, message_id, current_title):
        super().__init__()
        self.message_id = message_id
        self.title_input.default = current_title

    async def on_submit(self, interaction: discord.Interaction):
        new_title = self.title_input.value.strip()
        if not new_title:
            return

        async with get_db() as db:
            await db.execute(
                "UPDATE protected_items SET title = ? WHERE message_id = ?",
                (new_title, self.message_id),
            )
            await db.commit()

        await refresh_protected_post_embed(interaction.client, self.message_id)

        await interaction.response.send_message(
            f"✅ 标题已更新为：**{new_title}**", ephemeral=True
        )


class EditProtectionNoteModal(ui.Modal, title="修改作者提示"):
    note_input = ui.TextInput(
        label="新的作者提示",
        style=discord.TextStyle.paragraph,
        placeholder="请输入给下载者的说明...",
        max_length=1000,
        required=False,
    )

    def __init__(self, message_id, current_note):
        super().__init__()
        self.message_id = message_id
        self.note_input.default = current_note or ""

    async def on_submit(self, interaction: discord.Interaction):
        new_note = self.note_input.value.strip()

        async with get_db() as db:
            await db.execute(
                "UPDATE protected_items SET log = ? WHERE message_id = ?",
                (new_note, self.message_id),
            )
            await db.commit()

        await interaction.response.send_message("✅ 作者提示已更新！", ephemeral=True)


class EditProtectionPasswordModal(ui.Modal, title="设置新口令"):
    pw_input = ui.TextInput(
        label="设置新口令",
        placeholder="留空则保持原密码不变（如果是从无密码模式切换来，则必须输入）",
        min_length=2,
        max_length=20,
        required=False,
    )

    def __init__(self, message_id, new_mode):
        super().__init__()
        self.message_id = message_id
        self.new_mode = new_mode

    async def on_submit(self, interaction: discord.Interaction):
        new_pw = self.pw_input.value.strip()

        async with get_db() as db:
            if new_pw:
                # 更新模式和密码
                await db.execute(
                    "UPDATE protected_items SET unlock_type = ?, password = ? WHERE message_id = ?",
                    (self.new_mode, new_pw, self.message_id),
                )
            elif self.new_mode:  # 如果是单纯切换模式但不想改密码（前提是原先有密码，这里简单处理，如果没输密码就只更新模式，假设用户知道自己在做什么）
                await db.execute(
                    "UPDATE protected_items SET unlock_type = ? WHERE message_id = ?",
                    (self.new_mode, self.message_id),
                )
            await db.commit()

        await refresh_protected_post_embed(interaction.client, self.message_id)

        mode_text = "点赞+口令" if self.new_mode == "like_password" else "全套验证"
        msg = f"✅ 验证模式已切换为 **{mode_text}**"
        if new_pw:
            msg += f"\n新口令: ||{new_pw}||"
        await interaction.response.send_message(msg, ephemeral=True)


# --- 管理视图组件 ---
class ManageFilesSelectView(ui.View):
    def __init__(self, bot, owner_id, message_id, file_data, post_title):
        super().__init__(timeout=180)
        self.bot = bot
        self.owner_id = owner_id
        self.message_id = message_id
        self.file_data = file_data
        self.post_title = post_title
        self.selected_index = 0
        options = []
        for i, f in enumerate(file_data):
            ensure_file_entry_defaults(f)
            fname = f.get("filename", "unknown")
            options.append(
                discord.SelectOption(
                    label=f"{i + 1}. {fname[:90]}",
                    value=str(i),
                    description=(f.get("original_filename") or fname)[:100],
                    default=(i == 0),
                )
            )
        self.select = ui.Select(placeholder="选择要操作的文件...", options=options)
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def on_select(self, interaction: discord.Interaction):
        self.selected_index = int(self.select.values[0])
        for idx, option in enumerate(self.select.options):
            option.default = idx == self.selected_index
        entry = self.file_data[self.selected_index]
        await interaction.response.edit_message(
            content=f"当前选中：{build_file_line(entry, self.selected_index + 1)}",
            view=self,
        )

    @ui.button(label="改名", style=discord.ButtonStyle.secondary, row=1, emoji="✏️")
    async def rename_file(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(
            EditPublishedFileModal(self.message_id, self.selected_index, self.file_data)
        )

    @ui.button(label="替换文件", style=discord.ButtonStyle.primary, row=1, emoji="♻️")
    async def replace_file(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message(
            "请在当前频道 5 分钟内发送 1 条带附件或纯文本的消息（附件会取第一个，纯文本会转成 .txt 进行替换）。",
            ephemeral=True,
        )

        def check(msg: discord.Message):
            return (
                msg.author.id == self.owner_id
                and msg.channel.id == interaction.channel.id
                and (len(msg.attachments) > 0 or has_collectible_message_content(msg))
            )

        try:
            msg = await self.bot.wait_for("message", timeout=300, check=check)
        except asyncio.TimeoutError:
            return await interaction.followup.send(
                "⏰ 超时未收到可替换内容，已取消替换。", ephemeral=True
            )

        old_entry = ensure_file_entry_defaults(self.file_data[self.selected_index])
        if msg.attachments:
            attachment = msg.attachments[0]
            try:
                file_bytes = await attachment.read()
            except Exception as exc:
                return await interaction.followup.send(
                    f"❌ 读取附件失败：{exc}", ephemeral=True
                )
            new_original_name = get_attachment_original_name(attachment)
            try:
                stored_data = await upload_files_for_storage(
                    self.bot,
                    interaction.user,
                    f"管理替换: {self.post_title}",
                    [(file_bytes, new_original_name, {
                        "filename": new_original_name,
                        "original_filename": new_original_name,
                        "tag": old_entry.get("tag"),
                    })],
                )
            except Exception as exc:
                return await interaction.followup.send(
                    f"❌ 替换文件备份失败：{exc}", ephemeral=True
                )
            updated_entry = stored_data[0]
        else:
            text_attachment = build_text_cached_attachment(msg.content.strip())
            file_bytes = await text_attachment.read()
            new_original_name = text_attachment.filename
            updated_entry = ensure_file_entry_defaults(
                {
                    "strategy": "inline_text",
                    "filename": new_original_name,
                    "original_filename": new_original_name,
                    "tag": old_entry.get("tag"),
                    "text_content": file_bytes.decode("utf-8", errors="replace"),
                }
            )

        self.file_data[self.selected_index] = updated_entry
        ensure_file_entry_defaults(self.file_data[self.selected_index])

        async with get_db() as db:
            await db.execute(
                "UPDATE protected_items SET storage_urls = ? WHERE message_id = ?",
                (json.dumps(self.file_data), self.message_id),
            )
            await db.commit()

        await refresh_protected_post_embed(interaction.client, self.message_id)

        try:
            await msg.delete()
        except Exception:
            pass

        for idx, option in enumerate(self.select.options):
            name = self.file_data[idx].get("filename", "unknown")
            option.label = f"{idx + 1}. {name[:90]}"
            option.description = (
                self.file_data[idx].get("original_filename") or name
            )[:100]
            option.default = idx == self.selected_index

        try:
            await interaction.message.edit(
                content=f"当前选中：{build_file_line(self.file_data[self.selected_index], self.selected_index + 1)}",
                view=self,
            )
        except Exception:
            pass

        await interaction.followup.send(
            f"✅ 已替换为：{self.file_data[self.selected_index]['filename']}",
            ephemeral=True,
        )

    @ui.button(label="删除文件", style=discord.ButtonStyle.danger, row=1, emoji="🗑️")
    async def delete_file(self, interaction: discord.Interaction, button: ui.Button):
        if len(self.file_data) <= 1:
            return await interaction.response.send_message(
                "❌ 当前仅剩 1 个文件，无法单独删除。请使用“删除帖子”。",
                ephemeral=True,
            )

        removed = self.file_data.pop(self.selected_index)
        if self.selected_index >= len(self.file_data):
            self.selected_index = len(self.file_data) - 1

        async with get_db() as db:
            await db.execute(
                "UPDATE protected_items SET storage_urls = ? WHERE message_id = ?",
                (json.dumps(self.file_data), self.message_id),
            )
            await db.commit()

        await refresh_protected_post_embed(interaction.client, self.message_id)

        self.select.options = []
        for i, f in enumerate(self.file_data):
            ensure_file_entry_defaults(f)
            fname = f.get("filename", "unknown")
            self.select.options.append(
                discord.SelectOption(
                    label=f"{i + 1}. {fname[:90]}",
                    value=str(i),
                    description=(f.get("original_filename") or fname)[:100],
                    default=(i == self.selected_index),
                )
            )

        await interaction.response.edit_message(
            content=(
                f"✅ 已删除：{removed.get('filename', 'unknown')}\n"
                f"当前选中：{build_file_line(self.file_data[self.selected_index], self.selected_index + 1)}"
            ),
            view=self,
        )


class EditUnlockModeView(ui.View):
    def __init__(self, message_id):
        super().__init__(timeout=60)
        self.message_id = message_id

    async def update_mode(self, interaction, mode, needs_password=False):
        if needs_password:
            await interaction.response.send_modal(
                EditProtectionPasswordModal(self.message_id, mode)
            )
        else:
            async with get_db() as db:
                # 切换回不需要密码的模式时，建议清空密码字段或保留备用，这里仅更新 unlock_type
                await db.execute(
                    "UPDATE protected_items SET unlock_type = ? WHERE message_id = ?",
                    (mode, self.message_id),
                )
                await db.commit()

            await refresh_protected_post_embed(interaction.client, self.message_id)

            mode_text = "点赞解锁" if mode == "like" else "点赞+评论"
            await interaction.response.send_message(
                f"✅ 验证模式已切换为 **{mode_text}**", ephemeral=True
            )

    @ui.button(label="👍 点赞解锁", style=discord.ButtonStyle.primary)
    async def mode_like(self, i: discord.Interaction, b: ui.Button):
        await self.update_mode(i, "like")

    @ui.button(label="💬 点赞+评论", style=discord.ButtonStyle.primary)
    async def mode_like_comm(self, i: discord.Interaction, b: ui.Button):
        await self.update_mode(i, "like_comment")

    @ui.button(label="🔐 点赞+口令", style=discord.ButtonStyle.success)
    async def mode_like_pass(self, i: discord.Interaction, b: ui.Button):
        await self.update_mode(i, "like_password", True)

    @ui.button(label="🔐💬 全套验证", style=discord.ButtonStyle.success)
    async def mode_all(self, i: discord.Interaction, b: ui.Button):
        await self.update_mode(i, "like_comment_password", True)


class PostManagementView(ui.View):
    def __init__(
        self,
        bot,
        message_id,
        file_data,
        current_title,
        current_note,
        posts_rows=None,
    ):
        super().__init__(timeout=600)
        self.message_id = message_id
        self.bot = bot
        self.file_data = [ensure_file_entry_defaults(f) for f in file_data]
        self.current_title = current_title
        self.current_note = current_note
        self.posts_rows = posts_rows or []
        self.posts_map = {str(p["message_id"]): p for p in self.posts_rows}

        if len(self.posts_rows) >= 2:
            options = []
            for p in self.posts_rows[:25]:
                title = (p.get("title") or "无标题")[:90]
                dl_count = p.get("download_count", 0)
                message_id = p.get("message_id")
                options.append(
                    discord.SelectOption(
                        label=title,
                        value=str(message_id),
                        description=f"下载: {dl_count}次 | ID: {message_id}",
                        default=(message_id == self.message_id),
                    )
                )
            self.switch_post_select = ui.Select(
                placeholder="快捷切换到其他批次...",
                options=options,
                row=3,
            )
            self.switch_post_select.callback = self.on_switch_post
            self.add_item(self.switch_post_select)

    async def on_switch_post(self, interaction: discord.Interaction):
        mid_str = self.switch_post_select.values[0]
        row = self.posts_map.get(mid_str)
        if not row:
            return await interaction.response.send_message(
                "❌ 切换失败，请重试。", ephemeral=True
            )

        try:
            file_data = json.loads(row["storage_urls"])
        except Exception:
            file_data = []
        file_data = [ensure_file_entry_defaults(f) for f in file_data]

        current_title = row["title"]
        current_note = row["log"]
        embed = discord.Embed(
            title=f"🔧 管理: {current_title}",
            description="请选择操作：",
            color=0xFFD700,
        )
        view = PostManagementView(
            self.bot,
            row["message_id"],
            file_data,
            current_title,
            current_note,
            posts_rows=self.posts_rows,
        )
        await interaction.response.edit_message(embed=embed, view=view)

    # 第一排：内容修改
    @ui.button(label="改标题", style=discord.ButtonStyle.secondary, row=0, emoji="🏷️")
    async def edit_title(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(
            EditProtectionTitleModal(self.message_id, self.current_title)
        )

    @ui.button(label="改提示", style=discord.ButtonStyle.secondary, row=0, emoji="📝")
    async def edit_note(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(
            EditProtectionNoteModal(self.message_id, self.current_note)
        )

    @ui.button(label="单文件操作", style=discord.ButtonStyle.secondary, row=0, emoji="✏️")
    async def rename_files(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message(
            "请选择文件并执行操作（改名/替换/删除）：",
            view=ManageFilesSelectView(
                self.bot,
                interaction.user.id,
                self.message_id,
                self.file_data,
                self.current_title,
            ),
            ephemeral=True,
        )

    @ui.button(
        label="批量编辑文件", style=discord.ButtonStyle.secondary, row=0, emoji="🧩"
    )
    async def batch_edit_files(
        self, interaction: discord.Interaction, button: ui.Button
    ):
        view = PublishedBatchFileView(self.message_id, self.file_data)
        _, _, preview = view.build_preview()
        await interaction.response.send_message(preview, view=view, ephemeral=True)

    @ui.button(label="设置标签", style=discord.ButtonStyle.secondary, row=1, emoji="🏷️")
    async def set_tags(self, interaction: discord.Interaction, button: ui.Button):
        view = PublishedTagBatchView(self.message_id, self.file_data)
        _, _, preview = view.build_preview()
        await interaction.response.send_message(preview, view=view, ephemeral=True)

    @ui.button(label="追加附件", style=discord.ButtonStyle.primary, row=1, emoji="➕")
    async def append_files(self, interaction: discord.Interaction, button: ui.Button):
        cog = self.bot.get_cog("ProtectionCog")
        if not cog:
            return await interaction.response.send_message(
                "❌ 未找到 ProtectionCog，暂时无法启动追加会话。",
                ephemeral=True,
            )

        key = cog._session_key(interaction.user.id, interaction.channel.id)
        old_session = cog.upload_sessions.pop(key, None)
        if old_session and old_session.get("task"):
            old_session["task"].cancel()

        expires_at = datetime.now(TZ_SHANGHAI) + timedelta(
            seconds=UPLOAD_SESSION_TIMEOUT_SECONDS
        )
        task = self.bot.loop.create_task(
            cog._expire_upload_session(
                interaction.user.id, interaction.channel.id, expires_at
            )
        )

        async def finish_append_upload(done_interaction: discord.Interaction):
            session = cog.upload_sessions.pop(key, None)
            if not session:
                return await done_interaction.response.edit_message(
                    content="❌ 当前没有可提交的附件收集会话。",
                    embed=None,
                    view=None,
                )

            if session.get("task"):
                session["task"].cancel()

            source_messages = session["messages"]
            if not source_messages:
                return await done_interaction.response.edit_message(
                    content="❌ 还没有收集到任何可追加内容，请先发送带附件或纯文本的消息。",
                    embed=None,
                    view=None,
                )

            try:
                latest_row = await get_protected_item_row(self.message_id)
                latest_title = latest_row["title"] if latest_row else self.current_title
                attachments, _default_log = await collect_cached_attachments_from_messages(
                    source_messages
                )
                file_entries = []
                for att in attachments:
                    if getattr(att, "stored_entry", None):
                        file_entries.append(ensure_file_entry_defaults(dict(att.stored_entry)))
                    else:
                        original_name = get_attachment_original_name(att)
                        file_entries.append(
                            ensure_file_entry_defaults(
                                {
                                    "filename": original_name,
                                    "original_filename": original_name,
                                    "tag": None,
                                }
                            )
                        )
                stored_items = await build_storage_entries_from_attachments(
                    self.bot,
                    done_interaction.user,
                    f"管理追加: {latest_title}",
                    attachments,
                    file_entries,
                )
            except Exception as exc:
                return await done_interaction.response.edit_message(
                    content=f"❌ 追加附件失败：{exc}",
                    embed=None,
                    view=None,
                )

            self.file_data.extend(stored_items)
            async with get_db() as db:
                await db.execute(
                    "UPDATE protected_items SET storage_urls = ? WHERE message_id = ?",
                    (json.dumps(self.file_data), self.message_id),
                )
                await db.commit()

            await refresh_protected_post_embed(done_interaction.client, self.message_id)

            for msg in source_messages:
                try:
                    await msg.delete()
                except Exception:
                    pass

            await done_interaction.response.edit_message(
                content=f"✅ 已成功追加 **{len(stored_items)}** 个附件到当前已发布批次。",
                embed=None,
                view=None,
            )

        cog.upload_sessions[key] = {
            "messages": [],
            "reusable_attachments": [],
            "reusable_metadata": {},
            "expires_at": expires_at,
            "task": task,
            "finish_handler": finish_append_upload,
            "cancel_reason": "已取消本次附件追加。",
            "panel_title": "📥 追加附件收集中",
            "panel_description": (
                "接下来 5 分钟内，你在当前频道发送的带附件或纯文本消息都会追加到这条已发布附件里。\n"
                "全部发完后点击下方 `上传完毕` 即可写入当前批次。"
            ),
            "panel_note": "收集规则与发布附件一致：支持多条消息、多附件，纯文本会自动转成可保护的文本附件。",
        }

        view = UploadSessionControlView(cog, interaction.user.id, interaction.channel.id)
        await interaction.response.send_message(
            embed=view._build_embed(),
            view=view,
            ephemeral=True,
        )

    # 第二排：逻辑修改与删除
    @ui.button(label="⚙️ 修改验证方式", style=discord.ButtonStyle.primary, row=2)
    async def change_mode(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message(
            "请选择新的验证模式：",
            view=EditUnlockModeView(self.message_id),
            ephemeral=True,
        )

    @ui.button(label="🗑️ 删除帖子", style=discord.ButtonStyle.danger, row=2)
    async def delete_post(self, interaction: discord.Interaction, button: ui.Button):
        # 双重确认可以防止误删，但作为管理工具直接执行也行，这里直接执行
        async with get_db() as db:
            await db.execute(
                "DELETE FROM protected_items WHERE message_id = ?", (self.message_id,)
            )
            await db.commit()

        # 尝试删除 Discord 实际消息
        try:
            msg = await interaction.channel.fetch_message(self.message_id)
            await msg.delete()
        except:
            pass

        await interaction.response.edit_message(
            content="✅ 帖子已删除！相关记录已清理。", embed=None, view=None
        )


class PostSelectionView(ui.View):
    def __init__(self, bot, posts_rows):
        super().__init__(timeout=60)
        self.bot = bot
        self.posts_rows = posts_rows
        options = []
        for p in posts_rows:
            title = p["title"][:80]
            dl_count = p["download_count"]
            options.append(
                discord.SelectOption(
                    label=title,
                    value=str(p["message_id"]),
                    description=f"下载: {dl_count}次 | ID: {p['message_id']}",
                )
            )
        self.select = ui.Select(placeholder="选择要管理的帖子...", options=options)
        self.select.callback = self.on_select
        self.add_item(self.select)
        self.posts_map = {str(p["message_id"]): p for p in posts_rows}

    async def on_select(self, interaction: discord.Interaction):
        mid_str = self.select.values[0]
        row = self.posts_map[mid_str]

        try:
            file_data = json.loads(row["storage_urls"])
        except:
            file_data = []
        file_data = [ensure_file_entry_defaults(f) for f in file_data]

        # 将当前标题和提示传进去，方便回显
        current_title = row["title"]
        current_note = row["log"]

        embed = discord.Embed(
            title=f"🔧 管理: {current_title}",
            description="请选择操作：",
            color=0xFFD700,
        )

        # 实例化更新后的视图
        view = PostManagementView(
            self.bot,
            row["message_id"],
            file_data,
            current_title,
            current_note,
            posts_rows=self.posts_rows,
        )

        await interaction.response.edit_message(embed=embed, view=view)


class PostListView(ui.View):
    def __init__(self, bot, posts_rows):
        super().__init__(timeout=600)
        self.bot = bot
        self.posts = posts_rows
        self.selected_row = None
        options = []
        for p in self.posts:
            title = p["title"][:90]
            ts_str = datetime.fromisoformat(p["created_at"]).strftime("%m-%d %H:%M")
            options.append(
                discord.SelectOption(
                    label=title,
                    description=f"发布于: {ts_str}",
                    value=str(p["message_id"]),
                    emoji="📄",
                )
            )
        self.select_menu = ui.Select(
            placeholder="🔍 请选择要获取的附件...", options=options, row=0
        )
        self.select_menu.callback = self.on_select
        self.add_item(self.select_menu)

    async def on_select(self, interaction: discord.Interaction):
        selected_id = int(self.select_menu.values[0])
        self.selected_row = next(
            (p for p in self.posts if p["message_id"] == selected_id), None
        )
        if not self.selected_row:
            return await interaction.response.send_message(
                "选择出错，请重试。", ephemeral=True
            )
        self.btn_download.disabled = False
        try:
            file_data = json.loads(self.selected_row["storage_urls"])
            file_data = [ensure_file_entry_defaults(f) for f in file_data]
            file_list = "\n".join([f"📄 {build_file_line(f)}" for f in file_data])
        except:
            file_list = "解析错误"
        mode_map = {
            "like": "👍 点赞",
            "like_comment": "👍💬 点赞+评论",
            "like_password": "👍🔐 点赞+口令",
            "like_comment_password": "👍💬🔐 全套验证",
        }
        embed = discord.Embed(
            title=f"📂 {self.selected_row['title']}", color=discord.Color.green()
        )
        embed.add_field(name="📋 包含文件", value=file_list[:1000], inline=False)
        embed.add_field(
            name="🔑 获取条件",
            value=mode_map.get(self.selected_row["unlock_type"], "未知"),
            inline=False,
        )
        embed.set_footer(text="请点击下方按钮验证条件并下载")
        await interaction.response.edit_message(embed=embed, view=self)

    @ui.button(
        label="验证并获取",
        style=discord.ButtonStyle.success,
        emoji="🎁",
        disabled=True,
        row=1,
    )
    async def btn_download(self, interaction: discord.Interaction, button: ui.Button):
        if not self.selected_row:
            return
        row = self.selected_row
        unlock_type = row["unlock_type"]

        allowed, reject_msg = await check_rate_limit_and_report(
            interaction, self.bot, row
        )
        if not allowed:
            return await interaction.response.send_message(reject_msg, ephemeral=True)

        # 1. 检查密码模式
        if "password" in unlock_type:
            has_test_role = isinstance(
                interaction.user, discord.Member
            ) and interaction.user.get_role(TEST_ROLE_ID)
            # 如果是主人且没测试身份，直接给文件（跳过Delay，但建议还是打上水印以防万一）
            # 不过为了方便自己测试，保持原样逻辑给无水印版也行，这里我为了你的测试体验，保持了主人直通，但如果你希望测试水印功能，建议用小号下载
            if interaction.user.id == row["owner_id"] and not has_test_role:
                await interaction.response.defer(ephemeral=True, thinking=True)
                await deliver_protected_content(
                    interaction,
                    self.bot,
                    row,
                    owner_bypass=True,
                )
                return
            # 否则弹出密码框
            await interaction.response.send_modal(
                PasswordUnlockModal(row["password"], row, self.bot, unlock_type)
            )

        # 2. 普通模式 (验证点赞评论)
        else:
            await interaction.response.defer(ephemeral=True, thinking=True)
            success, msg = await check_requirements_common(
                interaction, unlock_type, row["owner_id"], row["message_id"]
            )
            if not success:
                return await interaction.followup.send(msg, ephemeral=True)

            # 验证通过后，进入注入流程
            await start_download_flow(interaction, self.bot, row)


class BumpButtonView(discord.ui.View):
    def __init__(self, bot, top_url: str | None = None):
        super().__init__(timeout=None)
        self.bot = bot
        self.top_url = top_url
        if self.top_url:
            self.add_item(
                discord.ui.Button(
                    label="回到顶部",
                    style=discord.ButtonStyle.link,
                    url=self.top_url,
                    row=0,
                )
            )

    def create_layout(self):
        """
        生成置底消息的参数. 返回一个包含视图实例的字典.
        """
        message_content = (
            "⬇️ **本频道有受保护的附件** ⬇️\n"
            "为了防止资源被聊天记录淹没，请点击下方按钮查看下载列表。\n"
            "如果本按钮失效，也可以使用`/保护附件 获取附件`命令获取。"
        )

        return {
            "content": message_content,
            "embed": None,
            "view": self,  # 直接返回当前实例
        }

    @discord.ui.button(
        label="获取本帖附件",
        style=discord.ButtonStyle.blurple,
        custom_id="bump_get_attachments",
        emoji="📥",
    )
    async def get_attachments_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        """
        按钮点击后的回调：查找附件并显示下拉菜单。
        """

        bot = interaction.client
        rows = await protection_db.get_items_in_channel(
            interaction.channel_id, limit=25
        )

        if not rows:
            return await interaction.response.send_message(
                "❌ 本频道当前没有任何受保护的附件记录。", ephemeral=True
            )

        view = PostListView(bot, rows)

        result_embed = discord.Embed(
            title="📂 附件获取列表",
            description=f"发现本频道有 **{len(rows)}** 个最近的附件包。\n请在下方下拉菜单中选择一个进行查看和下载。",
            color=0x87CEEB,
        )

        await interaction.response.send_message(
            embed=result_embed, view=view, ephemeral=True
        )
