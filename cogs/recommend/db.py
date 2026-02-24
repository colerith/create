import aiosqlite
from datetime import datetime

from ..core.db import get_db
from config import TZ_SHANGHAI

async def init_recommend_db():
    """在Cog加载时检查并创建所需表格"""
    async with get_db() as db:
        # 每日抽卡记录表
        await db.execute("""
            CREATE TABLE IF NOT EXISTS daily_gacha_records (
                user_id INTEGER PRIMARY KEY,
                last_draw_date TEXT
            )
        """)
        # 推荐面板消息ID记录表
        await db.execute("""
            CREATE TABLE IF NOT EXISTS recommend_panels (
                channel_id INTEGER PRIMARY KEY,
                message_id INTEGER NOT NULL
            )
        """)
        await db.commit()

async def check_user_drawn_today(user_id: int) -> bool:
    """检查用户今天是否已经抽过卡"""
    today_str = datetime.now(TZ_SHANGHAI).strftime("%Y-%m-%d")
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT 1 FROM daily_gacha_records WHERE user_id = ? AND last_draw_date = ?",
            (user_id, today_str)
        )
        return await cursor.fetchone() is not None

async def mark_user_drawn(user_id: int):
    """标记用户今天已抽卡"""
    today_str = datetime.now(TZ_SHANGHAI).strftime("%Y-%m-%d")
    async with get_db() as db:
        await db.execute(
            "INSERT OR REPLACE INTO daily_gacha_records (user_id, last_draw_date) VALUES (?, ?)",
            (user_id, today_str)
        )
        await db.commit()

async def set_panel_message(channel_id: int, message_id: int):
    """记录或更新一个频道的推荐面板消息ID"""
    async with get_db() as db:
        await db.execute(
            "INSERT OR REPLACE INTO recommend_panels (channel_id, message_id) VALUES (?, ?)",
            (channel_id, message_id)
        )
        await db.commit()

async def get_panel_message_id(channel_id: int) -> int | None:
    """获取一个频道的推荐面板消息ID"""
    async with get_db() as db:
        cursor = await db.execute("SELECT message_id FROM recommend_panels WHERE channel_id = ?", (channel_id,))
        row = await cursor.fetchone()
        return row[0] if row else None

async def remove_panel_message(channel_id: int):
    """如果消息失效, 从数据库中移除记录"""
    async with get_db() as db:
        await db.execute("DELETE FROM recommend_panels WHERE channel_id = ?", (channel_id,))
        await db.commit()
