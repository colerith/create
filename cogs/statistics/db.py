import aiosqlite
from datetime import datetime, timedelta
from ..core.db import get_db

async def init_statistics_db():
    """初始化统计功能所需的数据库表"""
    async with get_db() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS statistics_panels (
                message_id INTEGER PRIMARY KEY,
                panel_channel_id INTEGER NOT NULL,
                forum_channel_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bump_logs (
                thread_id INTEGER PRIMARY KEY,
                last_bump_timestamp TEXT NOT NULL
            )
        """)
        await db.commit()

# --- 统计面板管理 ---

async def add_statistics_panel(message_id: int, panel_channel_id: int, forum_channel_id: int, guild_id: int):
    """记录一个统计面板信息"""
    async with get_db() as db:
        await db.execute(
            "INSERT OR REPLACE INTO statistics_panels (message_id, panel_channel_id, forum_channel_id, guild_id) VALUES (?, ?, ?, ?)",
            (message_id, panel_channel_id, forum_channel_id, guild_id)
        )
        await db.commit()

async def get_all_statistics_panels():
    """获取所有已记录的统计面板"""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM statistics_panels")
        return await cursor.fetchall()

async def remove_statistics_panel(message_id: int):
    """当面板消息被删除时，从数据库移除记录"""
    async with get_db() as db:
        await db.execute("DELETE FROM statistics_panels WHERE message_id = ?", (message_id,))
        await db.commit()


# --- 顶帖记录管理 ---

async def log_thread_bump(thread_id: int):
    """记录一次顶帖操作"""
    timestamp = datetime.utcnow().isoformat()
    async with get_db() as db:
        await db.execute(
            "INSERT OR REPLACE INTO bump_logs (thread_id, last_bump_timestamp) VALUES (?, ?)",
            (thread_id, timestamp)
        )
        await db.commit()

async def get_recently_bumped_threads(days: int = 7) -> set:
    """获取最近X天内被顶过的帖子ID集合，用于排除"""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        threshold_date_str = (datetime.utcnow() - timedelta(days=days)).isoformat()
        cursor = await db.execute("SELECT thread_id FROM bump_logs WHERE last_bump_timestamp >= ?", (threshold_date_str,))
        rows = await cursor.fetchall()
        return {row['thread_id'] for row in rows}