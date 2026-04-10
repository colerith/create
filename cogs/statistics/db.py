import aiosqlite
import json
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
        # 3. 论坛帖子缓存表
        await db.execute("""
            CREATE TABLE IF NOT EXISTS forum_thread_cache (
                thread_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                forum_channel_id INTEGER NOT NULL,
                thread_name TEXT NOT NULL,
                thread_url TEXT,
                author_id INTEGER,
                author_name TEXT,
                created_at TEXT,
                last_message_at TEXT,
                likes INTEGER NOT NULL DEFAULT 0,
                comments INTEGER NOT NULL DEFAULT 0,
                score REAL NOT NULL DEFAULT 0,
                tags_json TEXT NOT NULL DEFAULT '[]',
                starter_image_url TEXT,
                is_archived INTEGER NOT NULL DEFAULT 0,
                last_synced_at TEXT NOT NULL
            )
        """)
        try:
            await db.execute(
                "ALTER TABLE forum_thread_cache ADD COLUMN starter_image_url TEXT"
            )
        except Exception:
            pass
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_forum_thread_cache_forum ON forum_thread_cache (forum_channel_id, is_archived)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_forum_thread_cache_synced ON forum_thread_cache (last_synced_at)"
        )
        # 4. 帖子里程碑状态表
        await db.execute("""
            CREATE TABLE IF NOT EXISTS thread_milestone_state (
                thread_id INTEGER PRIMARY KEY,
                last_like_milestone INTEGER NOT NULL DEFAULT 0,
                last_notified_at TEXT NOT NULL
            )
        """)
        await db.commit()
    print("✅ [StatisticsCog] 数据库表结构检查完毕。")


# --- 写操作 (Create / Update / Delete) ---


async def add_statistics_panel(
    message_id: int, panel_channel_id: int, forum_channel_id: int, guild_id: int
):
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
            (message_id, panel_channel_id, forum_channel_id, guild_id, now_iso),
        )
        await db.commit()


async def remove_statistics_panel(message_id: int):
    """根据消息ID移除一个统计面板记录。"""
    async with get_db() as db:
        await db.execute(
            "DELETE FROM statistics_panels WHERE message_id = ?", (message_id,)
        )
        await db.commit()


async def log_thread_bump(thread_id: int):
    """记录一次顶帖行为。"""
    async with get_db() as db:
        now_iso = datetime.now(TZ_SHANGHAI).isoformat()
        await db.execute(
            "INSERT OR REPLACE INTO bumped_threads (thread_id, last_bumped_at) VALUES (?, ?)",
            (thread_id, now_iso),
        )
        await db.commit()


async def upsert_forum_thread_snapshot(snapshot: dict):
    """写入或更新单个论坛帖子的缓存快照。"""
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO forum_thread_cache (
                thread_id, guild_id, forum_channel_id, thread_name, thread_url,
                author_id, author_name, created_at, last_message_at, likes,
                comments, score, tags_json, starter_image_url, is_archived, last_synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(thread_id) DO UPDATE SET
                guild_id = excluded.guild_id,
                forum_channel_id = excluded.forum_channel_id,
                thread_name = excluded.thread_name,
                thread_url = excluded.thread_url,
                author_id = excluded.author_id,
                author_name = excluded.author_name,
                created_at = excluded.created_at,
                last_message_at = excluded.last_message_at,
                likes = excluded.likes,
                comments = excluded.comments,
                score = excluded.score,
                tags_json = excluded.tags_json,
                starter_image_url = excluded.starter_image_url,
                is_archived = excluded.is_archived,
                last_synced_at = excluded.last_synced_at
            """,
            (
                snapshot["thread_id"],
                snapshot["guild_id"],
                snapshot["forum_channel_id"],
                snapshot["thread_name"],
                snapshot.get("thread_url"),
                snapshot.get("author_id"),
                snapshot.get("author_name"),
                snapshot.get("created_at"),
                snapshot.get("last_message_at"),
                snapshot.get("likes", 0),
                snapshot.get("comments", 0),
                snapshot.get("score", 0),
                json.dumps(snapshot.get("tags", []), ensure_ascii=False),
                snapshot.get("starter_image_url"),
                1 if snapshot.get("is_archived") else 0,
                snapshot["last_synced_at"],
            ),
        )
        await db.commit()


async def mark_missing_forum_threads_archived(
    forum_channel_id: int, active_thread_ids: list[int], synced_at: str
):
    """将本轮扫描未出现的论坛帖子标记为已归档。"""
    async with get_db() as db:
        if active_thread_ids:
            placeholders = ", ".join("?" for _ in active_thread_ids)
            await db.execute(
                f"""
                UPDATE forum_thread_cache
                SET is_archived = 1, last_synced_at = ?
                WHERE forum_channel_id = ? AND thread_id NOT IN ({placeholders})
                """,
                (synced_at, forum_channel_id, *active_thread_ids),
            )
        else:
            await db.execute(
                "UPDATE forum_thread_cache SET is_archived = 1, last_synced_at = ? WHERE forum_channel_id = ?",
                (synced_at, forum_channel_id),
            )
        await db.commit()


async def set_thread_like_milestone(thread_id: int, milestone: int):
    """记录帖子已播报的最高点赞里程碑。"""
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO thread_milestone_state (thread_id, last_like_milestone, last_notified_at)
            VALUES (?, ?, ?)
            ON CONFLICT(thread_id) DO UPDATE SET
                last_like_milestone = excluded.last_like_milestone,
                last_notified_at = excluded.last_notified_at
            """,
            (thread_id, milestone, datetime.now(TZ_SHANGHAI).isoformat()),
        )
        await db.commit()


# --- 读操作 (Read) ---


async def get_all_statistics_panels() -> list:
    """获取所有已记录的统计面板信息。"""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM statistics_panels")
        return await cursor.fetchall()


async def get_statistics_panel(message_id: int):
    """根据消息ID获取单个统计面板信息。"""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM statistics_panels WHERE message_id = ?", (message_id,)
        )
        return await cursor.fetchone()


async def get_cached_forum_threads(
    forum_channel_id: int, include_archived: bool = False
) -> list:
    """获取单个论坛的缓存帖子快照。"""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        if include_archived:
            cursor = await db.execute(
                "SELECT * FROM forum_thread_cache WHERE forum_channel_id = ?",
                (forum_channel_id,),
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM forum_thread_cache WHERE forum_channel_id = ? AND is_archived = 0",
                (forum_channel_id,),
            )
        rows = await cursor.fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["tags"] = json.loads(item.get("tags_json") or "[]")
            result.append(item)
        return result


async def get_cache_sync_summary(forum_channel_id: int):
    """获取论坛缓存同步摘要。"""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT COUNT(*) AS total_count, MAX(last_synced_at) AS last_synced_at
            FROM forum_thread_cache
            WHERE forum_channel_id = ?
            """,
            (forum_channel_id,),
        )
        return await cursor.fetchone()


async def get_thread_like_milestone(thread_id: int) -> int:
    """获取帖子已播报的最高点赞里程碑。"""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT last_like_milestone FROM thread_milestone_state WHERE thread_id = ?",
            (thread_id,),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0


async def get_threads_ready_for_like_milestones(min_likes: int = 1000) -> list:
    """获取所有达到点赞里程碑且可播报的缓存帖子。"""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT c.*, COALESCE(s.last_like_milestone, 0) AS last_like_milestone
            FROM forum_thread_cache c
            LEFT JOIN thread_milestone_state s ON s.thread_id = c.thread_id
            WHERE c.likes >= ?
            ORDER BY c.likes DESC, c.last_synced_at DESC
            """,
            (min_likes,),
        )
        rows = await cursor.fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["tags"] = json.loads(item.get("tags_json") or "[]")
            result.append(item)
        return result


async def get_recently_bumped_threads(days: int = 7) -> set:
    """获取最近指定天数内被顶过的帖子的ID集合。"""
    async with get_db() as db:
        since_date = (datetime.now(TZ_SHANGHAI) - timedelta(days=days)).isoformat()
        cursor = await db.execute(
            "SELECT thread_id FROM bumped_threads WHERE last_bumped_at >= ?",
            (since_date,),
        )
        rows = await cursor.fetchall()
        return {row[0] for row in rows}
