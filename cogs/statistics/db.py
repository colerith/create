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
        cursor = await db.execute("SELECT thread_id, last_bump_timestamp FROM bump_logs")
        rows = await cursor.fetchall()

        recently_bumped_ids = set()
        if not rows: return recently_bumped_ids

        now_utc = datetime.utcnow()
        threshold_date = now_utc - timedelta(days=days)

        for row in rows:
            # 确保时间字符串能被正确解析
            try:
                # fromisoformat 支持多种ISO 8601格式
                bump_time_utc = datetime.fromisoformat(row['last_bump_timestamp'].replace('Z', '+00:00'))
                # 统一转为无时区的UTC时间进行比较
                if bump_time_utc.replace(tzinfo=None) > threshold_date:
                    recently_bumped_ids.add(row['thread_id'])
            except ValueError:
                # 兼容可能不带时区信息的旧数据
                if len(row['last_bump_timestamp']) == 19: # YYYY-MM-DDTHH:MM:SS
                    bump_time_utc = datetime.strptime(row['last_bump_timestamp'], "%Y-%m-%dT%H:%M:%S")
                    if bump_time_utc > threshold_date:
                        recently_bumped_ids.add(row['thread_id'])

        return recently_bumped_ids