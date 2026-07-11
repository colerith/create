# cogs/exploration/db.py

import aiosqlite

from ..core.db import get_db

async def init_exploration_db():
    """初始化探索功能相关的数据库表"""
    async with get_db() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS panel_records (
                channel_id INTEGER NOT NULL,
                panel_type TEXT NOT NULL DEFAULT 'daily_report',
                message_id INTEGER NOT NULL,
                PRIMARY KEY (channel_id, panel_type)
            )
        """)
        cursor = await db.execute("PRAGMA table_info(panel_records)")
        panel_columns = await cursor.fetchall()
        channel_pk = next((col[5] for col in panel_columns if col[1] == "channel_id"), 0)
        panel_type_pk = next((col[5] for col in panel_columns if col[1] == "panel_type"), 0)
        if channel_pk and not panel_type_pk:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS panel_records_new (
                    channel_id INTEGER NOT NULL,
                    panel_type TEXT NOT NULL DEFAULT 'daily_report',
                    message_id INTEGER NOT NULL,
                    PRIMARY KEY (channel_id, panel_type)
                )
            """)
            await db.execute("""
                INSERT OR IGNORE INTO panel_records_new (channel_id, panel_type, message_id)
                SELECT channel_id, COALESCE(panel_type, 'daily_report'), message_id
                FROM panel_records
            """)
            await db.execute("DROP TABLE panel_records")
            await db.execute("ALTER TABLE panel_records_new RENAME TO panel_records")
        # 为 panel_type 创建索引可以加快查询速度，虽然此场景下非必须
        await db.execute("CREATE INDEX IF NOT EXISTS idx_panel_type ON panel_records (panel_type)")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS exploration_panel_pushes (
                push_key TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                panel_type TEXT NOT NULL,
                delivery_type TEXT NOT NULL,
                target_channel_id INTEGER,
                filter_channel_ids TEXT,
                message_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        try:
            await db.execute("ALTER TABLE exploration_panel_pushes ADD COLUMN filter_channel_ids TEXT")
        except:
            pass
        await db.execute("CREATE INDEX IF NOT EXISTS idx_exploration_panel_pushes_refresh ON exploration_panel_pushes (panel_type, delivery_type)")
        await db.commit()


def build_push_key(
    user_id: int,
    guild_id: int,
    panel_type: str,
    delivery_type: str,
    target_channel_id: int | None = None,
) -> str:
    target = "dm" if target_channel_id is None else str(target_channel_id)
    return f"{guild_id}:{user_id}:{panel_type}:{delivery_type}:{target}"

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
            """
            INSERT INTO panel_records (channel_id, panel_type, message_id)
            VALUES (?, ?, ?)
            ON CONFLICT(channel_id, panel_type) DO UPDATE SET message_id = excluded.message_id
            """,
            (channel_id, panel_type, message_id)
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


async def upsert_panel_push(
    user_id: int,
    guild_id: int,
    panel_type: str,
    delivery_type: str,
    message_id: int,
    timestamp: str,
    target_channel_id: int | None = None,
    filter_channel_ids: str | None = None,
):
    push_key = build_push_key(user_id, guild_id, panel_type, delivery_type, target_channel_id)
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO exploration_panel_pushes
            (push_key, user_id, guild_id, panel_type, delivery_type, target_channel_id, filter_channel_ids, message_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(push_key) DO UPDATE SET
                message_id = excluded.message_id,
                filter_channel_ids = excluded.filter_channel_ids,
                updated_at = excluded.updated_at
            """,
            (
                push_key,
                user_id,
                guild_id,
                panel_type,
                delivery_type,
                target_channel_id,
                filter_channel_ids,
                message_id,
                timestamp,
                timestamp,
            ),
        )
        await db.commit()


async def get_panel_push(
    user_id: int,
    guild_id: int,
    panel_type: str,
    delivery_type: str,
    target_channel_id: int | None = None,
):
    push_key = build_push_key(user_id, guild_id, panel_type, delivery_type, target_channel_id)
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM exploration_panel_pushes WHERE push_key = ?",
            (push_key,),
        )
        return await cursor.fetchone()


async def remove_panel_push(
    user_id: int,
    guild_id: int,
    panel_type: str,
    delivery_type: str,
    target_channel_id: int | None = None,
):
    push_key = build_push_key(user_id, guild_id, panel_type, delivery_type, target_channel_id)
    async with get_db() as db:
        await db.execute("DELETE FROM exploration_panel_pushes WHERE push_key = ?", (push_key,))
        await db.commit()


async def get_all_panel_pushes():
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM exploration_panel_pushes ORDER BY updated_at ASC")
        return await cursor.fetchall()
