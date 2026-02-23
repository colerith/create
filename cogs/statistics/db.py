import aiosqlite
from datetime import datetime, timedelta

# 从核心数据库模块获取连接
from ..core.db import get_db
from config import TZ_SHANGHAI

# --- 初始化与表结构 ---

async def init_statistics_db():
    """
    在Cog加载时被调用，确保所有需要的表都已创建。
    【核心修改】：在 statistics_panels 表中增加了 guild_id 字段。
    """
    async with get_db() as db:
        # 1. 统计面板信息表
        await db.execute("""
            CREATE TABLE IF NOT EXISTS statistics_panels (
                message_id INTEGER PRIMARY KEY,
                panel_channel_id INTEGER NOT NULL,
                forum_channel_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        # 2. 帖子顶帖记录表
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bumped_threads (
                thread_id INTEGER PRIMARY KEY,
                last_bumped_at TEXT NOT NULL
            )
        """)
        await db.commit()
    print("✅ [StatisticsCog] 数据库表结构检查完毕。")


# --- 写操作 (Create / Update / Delete) ---

async def add_statistics_panel(message_id: int, panel_channel_id: int, forum_channel_id: int, guild_id: int):
    """
    添加一个新的统计面板记录。
    【核心修改】：INSERT 语句现在包含 guild_id 字段和对应的第4个占位符。
    """
    async with get_db() as db:
        now_iso = datetime.now(TZ_SHANGHAI).isoformat()
        await db.execute(
            """
            INSERT INTO statistics_panels (message_id, panel_channel_id, forum_channel_id, guild_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (message_id, panel_channel_id, forum_channel_id, guild_id, now_iso)
        )
        await db.commit()

async def remove_statistics_panel(message_id: int):
    """根据消息ID移除一个统计面板记录。"""
    async with get_db() as db:
        await db.execute("DELETE FROM statistics_panels WHERE message_id = ?", (message_id,))
        await db.commit()

async def log_thread_bump(thread_id: int):
    """记录一次顶帖行为。"""
    async with get_db() as db:
        now_iso = datetime.now(TZ_SHANGHAI).isoformat()
        await db.execute(
            "INSERT OR REPLACE INTO bumped_threads (thread_id, last_bumped_at) VALUES (?, ?)",
            (thread_id, now_iso)
        )
        await db.commit()

# --- 读操作 (Read) ---

async def get_all_statistics_panels() -> list:
    """获取所有已记录的统计面板信息。"""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM statistics_panels")
        return await cursor.fetchall()

async def get_recently_bumped_threads(days: int = 7) -> set:
    """获取最近指定天数内被顶过的帖子的ID集合。"""
    async with get_db() as db:
        since_date = (datetime.now(TZ_SHANGHAI) - timedelta(days=days)).isoformat()
        cursor = await db.execute("SELECT thread_id FROM bumped_threads WHERE last_bumped_at >= ?", (since_date,))
        rows = await cursor.fetchall()
        return {row[0] for row in rows}