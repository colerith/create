# cogs/statistics/db.py

import aiosqlite
from datetime import datetime
from ..core.db import get_db
from config import TZ_SHANGHAI

async def init_statistics_db():
    """初始化统计面板和顶帖日志数据库表"""
    async with get_db() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS statistics_panels (
                message_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                target_forum_id INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bump_log (
                thread_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                last_bumped_at TEXT NOT NULL
            )
        """)
        await db.commit()

async def record_bump(thread_id: int, guild_id: int):
    """记录或更新一个帖子的顶帖时间"""
    timestamp = datetime.now(TZ_SHANGHAI).isoformat()
    async with get_db() as db:
        await db.execute(
            "INSERT OR REPLACE INTO bump_log (thread_id, guild_id, last_bumped_at) VALUES (?, ?, ?)",
            (thread_id, guild_id, timestamp)
        )
        await db.commit()

async def get_last_bumped_time(thread_id: int) -> datetime | None:
    """获取一个帖子的最后顶帖时间"""
    async with get_db() as db:
        cursor = await db.execute("SELECT last_bumped_at FROM bump_log WHERE thread_id = ?", (thread_id,))
        row = await cursor.fetchone()
        if row:
            return datetime.fromisoformat(row[0])
    return None

async def add_panel_record(message_id: int, channel_id: int, guild_id: int, target_forum_id: int):
    """添加一个新的统计面板记录"""
    timestamp = datetime.now(TZ_SHANGHAI).isoformat()
    async with get_db() as db:
        await db.execute(
            "INSERT OR REPLACE INTO statistics_panels (message_id, channel_id, guild_id, target_forum_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (message_id, channel_id, guild_id, target_forum_id, timestamp)
        )
        await db.commit()

async def get_all_panels():
    """获取所有已记录的统计面板信息"""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM statistics_panels")
        return await cursor.fetchall()

async def remove_panel_record(message_id: int):
    """根据消息ID移除一个统计面板记录"""
    async with get_db() as db:
        await db.execute("DELETE FROM statistics_panels WHERE message_id = ?", (message_id,))
        await db.commit()