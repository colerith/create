# cogs/recommend/db.py

import aiosqlite
from datetime import datetime

from ..core.db import get_db
from config import TZ_SHANGHAI

async def init_recommend_db():
    """在Cog加载时检查并创建抽卡记录表"""
    async with get_db() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS daily_gacha_records (
                user_id INTEGER PRIMARY KEY,
                last_draw_date TEXT
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