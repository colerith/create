# cogs/exploration/db.py

import aiosqlite
from ..core.db import get_db

async def init_exploration_db():
    """初始化探索功能相关的数据库表"""
    async with get_db() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS panel_records (
                channel_id INTEGER PRIMARY KEY,
                message_id INTEGER NOT NULL,
                panel_type TEXT NOT NULL DEFAULT 'daily_report'
            )
        """)
        # 为 panel_type 创建索引可以加快查询速度，虽然此场景下非必须
        await db.execute("CREATE INDEX IF NOT EXISTS idx_panel_type ON panel_records (panel_type)")
        await db.commit()

async def get_panel_message_id(channel_id: int, panel_type: str = 'daily_report') -> int | None:
    """根据频道ID和面板类型获取消息ID"""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT message_id FROM panel_records WHERE channel_id = ? AND panel_type = ?",
            (channel_id, panel_type)
        )
        row = await cursor.fetchone()
        return row[0] if row else None

async def set_panel_message_id(channel_id: int, message_id: int, panel_type: str = 'daily_report'):
    """设置或更新面板的消息ID"""
    async with get_db() as db:
        await db.execute(
            "INSERT OR REPLACE INTO panel_records (channel_id, message_id, panel_type) VALUES (?, ?, ?)",
            (channel_id, message_id, panel_type)
        )
        await db.commit()

async def remove_panel_record(channel_id: int, panel_type: str = 'daily_report'):
    """移除面板记录"""
    async with get_db() as db:
        await db.execute(
            "DELETE FROM panel_records WHERE channel_id = ? AND panel_type = ?",
            (channel_id, panel_type)
        )
        await db.commit()
