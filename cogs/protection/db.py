# cogs/protection/db.py

import json

import aiosqlite

from ..core.db import get_db


# --- 写操作 (Create / Update) ---

async def add_like(user_id: int, message_id: int):
    """为用户在特定消息上添加点赞记录。"""
    async with get_db() as db:
        await db.execute(
            "INSERT OR IGNORE INTO user_likes (user_id, message_id) VALUES (?, ?)",
            (user_id, message_id),
        )
        await db.commit()


async def add_or_update_comment(user_id: int, thread_id: int, content: str):
    """添加或更新用户评论记录。"""
    async with get_db() as db:
        await db.execute(
            "INSERT OR REPLACE INTO user_comments (user_id, message_id, content) VALUES (?, ?, ?)",
            (user_id, thread_id, content[:50]),
        )
        await db.commit()


async def record_download_log(user_id: int, message_id: int, title: str, filenames: list, timestamp: str):
    """记录普通下载行为。"""
    async with get_db() as db:
        await db.execute(
            "INSERT INTO download_log (user_id, message_id, title, filenames, timestamp) VALUES (?, ?, ?, ?, ?)",
            (user_id, message_id, title, json.dumps(filenames), timestamp),
        )
        await db.commit()


async def record_download_rate_warning(
    user_id: int,
    message_id: int | None,
    title: str | None,
    timestamp: str,
    warning_type: str = "rate_limit",
):
    """记录下载速率警告行为。"""
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO download_rate_warning_log
            (user_id, message_id, title, warning_type, timestamp)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, message_id, title, warning_type, timestamp),
        )
        await db.commit()


async def record_attachment_update_publish_log(
    owner_id: int,
    guild_id: int | None,
    channel_id: int,
    protected_message_id: int,
    update_message_id: int,
    title: str | None,
    update_log: str,
    timestamp: str,
    mention_users: bool = False,
):
    """记录通过 bot 发布保护附件时同步发送更新日志的行为。"""
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO attachment_update_publish_log
            (
                owner_id,
                guild_id,
                channel_id,
                protected_message_id,
                update_message_id,
                title,
                update_log,
                mention_users,
                timestamp
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                owner_id,
                guild_id,
                channel_id,
                protected_message_id,
                update_message_id,
                title,
                update_log,
                int(mention_users),
                timestamp,
            ),
        )
        await db.commit()


async def get_attachment_update_publish_logs_since(
    timestamp: str,
    guild_id: int | None = None,
    limit: int = 200,
):
    """获取指定时间后通过 bot 发布的附件更新日志。"""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        if guild_id is None:
            cursor = await db.execute(
                """
                SELECT *
                FROM attachment_update_publish_log
                WHERE timestamp >= ?
                ORDER BY timestamp DESC, log_id DESC
                LIMIT ?
                """,
                (timestamp, limit),
            )
        else:
            cursor = await db.execute(
                """
                SELECT *
                FROM attachment_update_publish_log
                WHERE timestamp >= ? AND (guild_id = ? OR guild_id IS NULL)
                ORDER BY timestamp DESC, log_id DESC
                LIMIT ?
                """,
                (timestamp, guild_id, limit),
            )
        return await cursor.fetchall()


async def log_file_trace(
    trace_id: str,
    user_id: int,
    guild_id: int,
    channel_id: int,
    message_id: int,
    filename: str,
    timestamp: str,
):
    """记录文件溯源指纹信息。"""
    async with get_db() as db:
        await db.execute(
            "INSERT INTO file_traces (trace_id, user_id, guild_id, channel_id, message_id, filename, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (trace_id, user_id, guild_id, channel_id, message_id, filename, timestamp),
        )
        await db.commit()


async def add_bump_config(channel_id: int):
    """添加自动置顶配置。"""
    async with get_db() as db:
        await db.execute("INSERT OR IGNORE INTO bump_config (channel_id) VALUES (?)", (channel_id,))
        await db.commit()


async def set_sticky_message(channel_id: int, message_id: int):
    """设置或更新置底消息ID。"""
    async with get_db() as db:
        await db.execute(
            "INSERT OR REPLACE INTO sticky_messages (channel_id, message_id) VALUES (?, ?)",
            (channel_id, message_id),
        )
        await db.commit()


async def remove_sticky_message(channel_id: int):
    """移除置底消息ID记录。"""
    async with get_db() as db:
        await db.execute("DELETE FROM sticky_messages WHERE channel_id = ?", (channel_id,))
        await db.commit()


# --- 删操作 (Delete) ---

async def remove_like(user_id: int, message_id: int):
    """移除用户点赞记录。"""
    async with get_db() as db:
        await db.execute("DELETE FROM user_likes WHERE user_id = ? AND message_id = ?", (user_id, message_id))
        await db.commit()


async def remove_bump_config(channel_id: int):
    """移除自动置底配置。"""
    async with get_db() as db:
        await db.execute("DELETE FROM bump_config WHERE channel_id = ?", (channel_id,))
        await db.commit()


# --- 读操作 (Read) ---

async def get_all_bump_configs():
    """获取所有自动置底频道配置。"""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT channel_id FROM bump_config")
        return await cursor.fetchall()


async def get_trace_record(trace_id: str):
    """根据溯源ID查询记录。"""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM file_traces WHERE trace_id = ?", (trace_id,))
        return await cursor.fetchone()


async def get_user_downloads_since(user_id: int, timestamp: str):
    """获取用户自某个时间点以来的下载记录。"""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT title, filenames, timestamp FROM download_log WHERE user_id = ? AND timestamp >= ? ORDER BY timestamp DESC",
            (user_id, timestamp),
        )
        return await cursor.fetchall()


async def count_user_download_rate_warnings_since(
    user_id: int, timestamp: str, warning_type: str = "rate_limit"
) -> int:
    """统计用户自某个时间点以来触发的下载速率警告次数。"""
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT COUNT(*)
            FROM download_rate_warning_log
            WHERE user_id = ? AND timestamp >= ? AND warning_type = ?
            """,
            (user_id, timestamp, warning_type),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0


async def get_items_in_channel(channel_id: int, limit: int = 100):
    """获取频道内受保护附件列表。"""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM protected_items WHERE channel_id = ? ORDER BY created_at DESC LIMIT ?",
            (channel_id, limit),
        )
        return await cursor.fetchall()


async def get_user_items_in_channel(user_id: int, channel_id: int):
    """获取用户在频道内发布的受保护附件。"""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM protected_items WHERE channel_id = ? AND owner_id = ?",
            (channel_id, user_id),
        )
        return await cursor.fetchall()


async def get_sticky_message_id(channel_id: int):
    """获取置底消息ID。"""
    async with get_db() as db:
        cursor = await db.execute("SELECT message_id FROM sticky_messages WHERE channel_id = ?", (channel_id,))
        row = await cursor.fetchone()
        return row[0] if row else None


async def count_user_download_logs(user_id: int) -> int:
    """获取用户历史下载总记录数。"""
    async with get_db() as db:
        cursor = await db.execute("SELECT COUNT(*) FROM download_log WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        return row[0] if row else 0


async def get_user_download_logs_page(user_id: int, limit: int = 10, offset: int = 0):
    """分页获取用户历史下载记录，附带原帖频道信息。"""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT
                dl.log_id,
                dl.user_id,
                dl.message_id,
                dl.title,
                dl.filenames,
                dl.timestamp,
                pi.channel_id
            FROM download_log dl
            LEFT JOIN protected_items pi ON dl.message_id = pi.message_id
            WHERE dl.user_id = ?
            ORDER BY dl.timestamp DESC, dl.log_id DESC
            LIMIT ? OFFSET ?
            """,
            (user_id, limit, offset),
        )
        return await cursor.fetchall()


async def get_user_library_items(user_id: int, limit: int = 100):
    """获取用户下载过、点赞过或评论过的保护帖，并把最近更新过的排在前面。"""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            WITH user_items AS (
                SELECT message_id, MAX(timestamp) AS touched_at, '下载' AS source
                FROM download_log
                WHERE user_id = ?
                GROUP BY message_id
                UNION ALL
                SELECT message_id, MAX(timestamp) AS touched_at, '点赞' AS source
                FROM user_likes
                WHERE user_id = ?
                GROUP BY message_id
                UNION ALL
                SELECT message_id, MAX(timestamp) AS touched_at, '评论' AS source
                FROM user_comments
                WHERE user_id = ?
                GROUP BY message_id
            ),
            grouped_items AS (
                SELECT message_id, MAX(touched_at) AS touched_at, GROUP_CONCAT(source) AS sources
                FROM user_items
                GROUP BY message_id
            )
            SELECT
                pi.message_id,
                pi.channel_id,
                pi.owner_id,
                pi.title,
                pi.created_at,
                gi.touched_at,
                gi.sources,
                (
                    SELECT MAX(a.timestamp)
                    FROM attachment_update_publish_log a
                    WHERE a.protected_message_id = pi.message_id
                ) AS latest_update_at,
                (
                    SELECT a.update_message_id
                    FROM attachment_update_publish_log a
                    WHERE a.protected_message_id = pi.message_id
                    ORDER BY a.timestamp DESC, a.log_id DESC
                    LIMIT 1
                ) AS latest_update_message_id,
                (
                    SELECT a.update_log
                    FROM attachment_update_publish_log a
                    WHERE a.protected_message_id = pi.message_id
                    ORDER BY a.timestamp DESC, a.log_id DESC
                    LIMIT 1
                ) AS latest_update_log
            FROM grouped_items gi
            JOIN protected_items pi ON pi.message_id = gi.message_id
            ORDER BY
                latest_update_at IS NOT NULL DESC,
                latest_update_at DESC,
                gi.touched_at DESC
            LIMIT ?
            """,
            (user_id, user_id, user_id, limit),
        )
        return await cursor.fetchall()


async def get_user_published_items(user_id: int, limit: int = 200):
    """获取用户发布过保护附件的帖子汇总。"""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT
                pi.channel_id,
                pi.owner_id,
                COUNT(*) AS attachment_count,
                MAX(pi.message_id) AS latest_message_id,
                MIN(pi.created_at) AS first_created_at,
                MAX(pi.created_at) AS latest_created_at,
                (
                    SELECT COUNT(*)
                    FROM user_likes ul
                    JOIN protected_items p2 ON p2.message_id = ul.message_id
                    WHERE p2.owner_id = ? AND p2.channel_id = pi.channel_id
                ) AS like_count,
                (
                    SELECT COUNT(*)
                    FROM user_comments uc
                    JOIN protected_items p3 ON p3.message_id = uc.message_id
                    WHERE p3.owner_id = ? AND p3.channel_id = pi.channel_id
                ) AS comment_count
            FROM protected_items
            pi
            WHERE pi.owner_id = ?
            GROUP BY pi.channel_id, pi.owner_id
            ORDER BY latest_created_at DESC
            LIMIT ?
            """,
            (user_id, user_id, user_id, limit),
        )
        return await cursor.fetchall()


async def get_user_published_thread_stats(user_id: int, channel_ids: list[int]):
    """按帖子线程统计用户通过 bot 发布的保护附件数量。"""
    if not channel_ids:
        return {}

    placeholders = ",".join("?" for _ in channel_ids)
    params = [user_id, *channel_ids]
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"""
            SELECT
                pi.channel_id,
                COUNT(*) AS attachment_count
            FROM protected_items pi
            WHERE pi.owner_id = ? AND pi.channel_id IN ({placeholders})
            GROUP BY pi.channel_id
            """,
            params,
        )
        rows = await cursor.fetchall()
        return {row["channel_id"]: row for row in rows}


async def count_suspicious_users_in_window(since_ts: str, min_downloads: int) -> int:
    """统计时间窗口内达到阈值下载次数的用户数。"""
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT COUNT(*)
            FROM (
                SELECT user_id
                FROM download_log
                WHERE timestamp >= ?
                GROUP BY user_id
                HAVING COUNT(*) >= ?
            ) t
            """,
            (since_ts, min_downloads),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0


async def get_suspicious_users_in_window(
    since_ts: str, min_downloads: int, limit: int = 10, offset: int = 0
):
    """分页获取时间窗口内短时高频下载用户。"""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT
                dl.user_id,
                COUNT(*) AS download_count,
                COUNT(DISTINCT dl.message_id) AS unique_posts,
                MAX(dl.timestamp) AS latest_timestamp
            FROM download_log dl
            WHERE dl.timestamp >= ?
            GROUP BY dl.user_id
            HAVING COUNT(*) >= ?
            ORDER BY download_count DESC, latest_timestamp DESC
            LIMIT ? OFFSET ?
            """,
            (since_ts, min_downloads, limit, offset),
        )
        return await cursor.fetchall()


async def get_recent_download_history(user_id: int, limit: int = 10):
    """获取用户最近下载历史，包含来源频道。"""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT
                dl.log_id,
                dl.user_id,
                dl.message_id,
                dl.title,
                dl.filenames,
                dl.timestamp,
                pi.channel_id
            FROM download_log dl
            LEFT JOIN protected_items pi ON dl.message_id = pi.message_id
            WHERE dl.user_id = ?
            ORDER BY dl.timestamp DESC, dl.log_id DESC
            LIMIT ?
            """,
            (user_id, limit),
        )
        return await cursor.fetchall()
