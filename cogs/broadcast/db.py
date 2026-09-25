"""小时简报的发送记录，以及现有论坛缓存/更新日志的只读查询。"""

import aiosqlite
import json

from ..core.db import get_db


async def init_db():
    async with get_db() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS hourly_broadcasts (
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                window_end TEXT NOT NULL,
                message_id INTEGER,
                recommendation_date TEXT,
                PRIMARY KEY (guild_id, channel_id, window_end)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS hourly_broadcast_threads (
                thread_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                forum_channel_id INTEGER NOT NULL,
                thread_name TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_hourly_broadcast_threads_guild_time
            ON hourly_broadcast_threads (guild_id, created_at)
        """)
        await conn.commit()


async def record_thread(thread):
    # 单独保留新帖事件，避免短时间内归档的帖子在统计缓存刷新前消失。
    async with get_db() as conn:
        await conn.execute("""
            INSERT OR REPLACE INTO hourly_broadcast_threads
            (thread_id, guild_id, forum_channel_id, thread_name, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (thread.id, thread.guild.id, thread.parent_id, thread.name, thread.created_at.isoformat()))
        await conn.commit()


async def get_inputs(guild_id, start, end):
    async with get_db() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute("""
            SELECT thread_id, guild_id, forum_channel_id, thread_name, created_at,
                   likes, comments, last_synced_at, author_id, author_name, tags_json, is_pinned
            FROM forum_thread_cache WHERE guild_id = ?
        """, (guild_id,))
        cached = [dict(row) for row in await cursor.fetchall()]
        for row in cached:
            row["tags"] = json.loads(row.pop("tags_json") or "[]")
        cursor = await conn.execute("""
            SELECT * FROM hourly_broadcast_threads
            WHERE guild_id = ? AND julianday(created_at) >= julianday(?)
                AND julianday(created_at) < julianday(?)
        """, (guild_id, start.isoformat(), end.isoformat()))
        events = [dict(row) for row in await cursor.fetchall()]
        cursor = await conn.execute("""
            SELECT channel_id, title, update_message_id, timestamp, log_id
            FROM attachment_update_publish_log
            WHERE guild_id = ? AND julianday(timestamp) >= julianday(?)
                AND julianday(timestamp) < julianday(?)
            ORDER BY julianday(timestamp) DESC, log_id DESC
        """, (guild_id, start.isoformat(), end.isoformat()))
        updates = [dict(row) for row in await cursor.fetchall()]
    return cached, events, updates


async def is_processed(guild_id, channel_id, end):
    async with get_db() as conn:
        cursor = await conn.execute("""
            SELECT 1 FROM hourly_broadcasts
            WHERE guild_id = ? AND channel_id = ? AND window_end = ?
        """, (guild_id, channel_id, end.isoformat()))
        return await cursor.fetchone() is not None


async def get_record(guild_id, channel_id, end):
    async with get_db() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute("""
            SELECT * FROM hourly_broadcasts
            WHERE guild_id = ? AND channel_id = ? AND window_end = ?
        """, (guild_id, channel_id, end.isoformat()))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def recommendation_sent(guild_id, channel_id, date):
    async with get_db() as conn:
        cursor = await conn.execute("""
            SELECT 1 FROM hourly_broadcasts
            WHERE guild_id = ? AND channel_id = ? AND recommendation_date = ? LIMIT 1
        """, (guild_id, channel_id, date))
        return await cursor.fetchone() is not None


async def mark_processed(guild_id, channel_id, end, message_id=None, recommendation_date=None):
    async with get_db() as conn:
        await conn.execute("""
            INSERT INTO hourly_broadcasts
            (guild_id, channel_id, window_end, message_id, recommendation_date)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, channel_id, window_end) DO UPDATE SET
                message_id = excluded.message_id,
                recommendation_date = excluded.recommendation_date
            WHERE hourly_broadcasts.message_id IS NULL AND excluded.message_id IS NOT NULL
        """, (guild_id, channel_id, end.isoformat(), message_id, recommendation_date))
        # 事件仅用于补齐最近的新帖；历史统计仍由原有缓存负责。
        await conn.execute("""
            DELETE FROM hourly_broadcast_threads
            WHERE guild_id = ? AND julianday(created_at) < julianday(?, '-2 days')
        """, (guild_id, end.isoformat()))
        await conn.commit()
