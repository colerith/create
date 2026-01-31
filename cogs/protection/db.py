import aiosqlite
from database import get_db

async def init_likes_db():
    async with get_db() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS cached_likes (
                message_id INTEGER,
                user_id INTEGER,
                PRIMARY KEY (message_id, user_id)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_likes ON cached_likes (message_id, user_id)")
        
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_likes (
                user_id INTEGER, message_id INTEGER, 
                PRIMARY KEY (user_id, message_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_comments (
                user_id INTEGER, message_id INTEGER, content TEXT,
                PRIMARY KEY (user_id, message_id)
            )
        """)
        await db.commit()

async def record_download_log(user_id, message_id, title, filenames, timestamp):
    async with get_db() as db:
        # 封装日志写入逻辑
        await db.execute(
            "INSERT INTO download_log (user_id, message_id, title, filenames, timestamp) VALUES (?, ?, ?, ?, ?)",
            (user_id, message_id, title, filenames, timestamp)
        )
        await db.commit()
