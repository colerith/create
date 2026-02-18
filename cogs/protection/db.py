# cogs/protection/db.py

import aiosqlite
import json
from datetime import datetime
from ..core.db import get_db
from config import TZ_SHANGHAI

# --- 写操作 (Create / Update) ---

async def add_like(user_id: int, message_id: int):
    """为用户在特定消息上添加点赞记录"""
    async with get_db() as db:
        await db.execute("INSERT OR IGNORE INTO user_likes (user_id, message_id) VALUES (?, ?)", (user_id, message_id))
        await db.commit()

async def add_or_update_comment(user_id: int, thread_id: int, content: str):
    """添加或更新用户的评论记录"""
    async with get_db() as db:
        await db.execute("INSERT OR REPLACE INTO user_comments (user_id, message_id, content) VALUES (?, ?, ?)", (user_id, thread_id, content[:50]))
        await db.commit()

async def record_download_log(user_id: int, message_id: int, title: str, filenames: list, timestamp: str):
    """记录普通的下载行为"""
    async with get_db() as db:
        await db.execute(
            "INSERT INTO download_log (user_id, message_id, title, filenames, timestamp) VALUES (?, ?, ?, ?, ?)",
            (user_id, message_id, title, json.dumps(filenames), timestamp)
        )
        await db.commit()

async def log_file_trace(trace_id: str, user_id: int, guild_id: int, channel_id: int, message_id: int, filename: str, timestamp: str):
    """记录文件的溯源指纹信息"""
    async with get_db() as db:
        await db.execute(
            "INSERT INTO file_traces (trace_id, user_id, guild_id, channel_id, message_id, filename, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (trace_id, user_id, guild_id, channel_id, message_id, filename, timestamp)
        )
        await db.commit()

async def add_bump_config(channel_id: int):
    """添加自动置顶配置"""
    async with get_db() as db:
        await db.execute("INSERT OR IGNORE INTO bump_config (channel_id) VALUES (?)", (channel_id,))
        await db.commit()

async def set_sticky_message(channel_id: int, message_id: int):
    """设置或更新置底消息ID"""
    async with get_db() as db:
        await db.execute("INSERT OR REPLACE INTO sticky_messages (channel_id, message_id) VALUES (?, ?)", (channel_id, message_id))
        await db.commit()

# --- 删操作 (Delete) ---

async def remove_like(user_id: int, message_id: int):
    """移除用户的点赞记录"""
    async with get_db() as db:
        await db.execute("DELETE FROM user_likes WHERE user_id = ? AND message_id = ?", (user_id, message_id))
        await db.commit()

async def remove_bump_config(channel_id: int):
    """移除自动置底配置"""
    async with get_db() as db:
        await db.execute("DELETE FROM bump_config WHERE channel_id = ?", (channel_id,))
        await db.commit()

# --- 读操作 (Read) ---

async def get_all_bump_configs():
    """获取所有自动置底的频道配置"""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT channel_id FROM bump_config")
        return await cursor.fetchall()

async def get_trace_record(trace_id: str):
    """根据溯源ID查询记录"""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM file_traces WHERE trace_id = ?", (trace_id,))
        return await cursor.fetchone()

async def get_user_downloads_since(user_id: int, timestamp: str):
    """获取用户自某个时间点以来的下载记录"""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT title, filenames, timestamp FROM download_log WHERE user_id = ? AND timestamp >= ? ORDER BY timestamp DESC", (user_id, timestamp))
        return await cursor.fetchall()

async def get_items_in_channel(channel_id: int, limit: int = 100):
    """获取频道内受保护的附件列表"""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM protected_items WHERE channel_id = ? ORDER BY created_at DESC LIMIT ?", (channel_id, limit))
        return await cursor.fetchall()

async def get_user_items_in_channel(user_id: int, channel_id: int):
    """获取特定用户在频道内发布的附件"""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM protected_items WHERE channel_id = ? AND owner_id = ?", (channel_id, user_id))
        return await cursor.fetchall()

async def get_sticky_message_id(channel_id: int):
    """获取置底消息的ID"""
    async with get_db() as db:
        cursor = await db.execute("SELECT message_id FROM sticky_messages WHERE channel_id = ?", (channel_id,))
        row = await cursor.fetchone()
        return row[0] if row else None