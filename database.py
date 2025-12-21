# database.py

import aiosqlite

DB_NAME = "chimidan.db"

async def init_db():
    print("🔄正在检查并初始化数据库...")
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        
        # 1. 保护贴主表
        await db.execute("""
            CREATE TABLE IF NOT EXISTS protected_items (
                message_id INTEGER PRIMARY KEY, channel_id INTEGER, owner_id INTEGER,
                unlock_type TEXT, storage_urls TEXT, title TEXT, log TEXT, password TEXT,
                created_at TEXT, download_count INTEGER DEFAULT 0
            )
        """)
        
        # 2. 点赞记录表
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_likes (
                user_id INTEGER, 
                message_id INTEGER,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, message_id)
            )
        """)
        
        # 3. 评论记录表
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_comments (
                user_id INTEGER, 
                message_id INTEGER, 
                content TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, 
                PRIMARY KEY (user_id, message_id)
            )
        """)

        # 4. 下载日志表
        await db.execute("""
            CREATE TABLE IF NOT EXISTS download_log (
                log_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                title TEXT, filenames TEXT, timestamp TEXT NOT NULL
            )
        """)
        
        try: 
            await db.execute("ALTER TABLE protected_items ADD COLUMN created_at TEXT")
        except Exception: 
            pass 
        
        await db.commit()
    print("✅ 数据库初始化完成，表结构已就绪。")

def get_db():
    return aiosqlite.connect(DB_NAME)
