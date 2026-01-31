import aiosqlite
from database import get_db

async def init_likes_db():
    """
    初始化所有业务相关的数据表
    建议在 bot 启动时的 on_ready 中调用一次
    """
    async with get_db() as db:
        # 1. 缓存点赞关系表
        await db.execute("""
            CREATE TABLE IF NOT EXISTS cached_likes (
                message_id INTEGER,
                user_id INTEGER,
                PRIMARY KEY (message_id, user_id)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_likes ON cached_likes (message_id, user_id)")

        # 2. 用户点赞记录表
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_likes (
                user_id INTEGER, message_id INTEGER,
                PRIMARY KEY (user_id, message_id)
            )
        """)

        # 3. 用户评论记录表（原有）
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_comments (
                user_id INTEGER, message_id INTEGER, content TEXT,
                PRIMARY KEY (user_id, message_id)
            )
        """)

        # 4. ===溯源追踪记录表 ===
        await db.execute("""
            CREATE TABLE IF NOT EXISTS file_traces (
                trace_id TEXT PRIMARY KEY,
                user_id INTEGER,
                guild_id INTEGER,
                channel_id INTEGER,
                message_id INTEGER,
                filename TEXT,
                created_at TEXT
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_file_traces_user ON file_traces (user_id)")

        # 5. 确保下载日志表存在
        await db.execute("""
            CREATE TABLE IF NOT EXISTS download_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                message_id INTEGER,
                title TEXT,
                filenames TEXT,
                timestamp TEXT
            )
        """)

        await db.commit()

async def record_download_log(user_id, message_id, title, filenames, timestamp):
    """记录普通的下载行为（用于统计额度等）"""
    async with get_db() as db:
        await db.execute(
            "INSERT INTO download_log (user_id, message_id, title, filenames, timestamp) VALUES (?, ?, ?, ?, ?)",
            (user_id, message_id, title, filenames, timestamp)
        )
        await db.commit()

async def log_file_trace(trace_id, user_id, guild_id, channel_id, message_id, filename, timestamp):
    """
    记录文件的溯源指纹信息
    当你把带水印的文件发给用户时，调用此函数
    """
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO file_traces
            (trace_id, user_id, guild_id, channel_id, message_id, filename, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (trace_id, user_id, guild_id, channel_id, message_id, filename, timestamp)
        )
        await db.commit()
