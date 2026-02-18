# core/db.py

import aiosqlite

DB_NAME = "chimidan.db"

async def init_db():
    """
    【统一入口】检查并初始化所有数据表。
    在机器人 on_ready 事件中调用此函数一次即可。
    """
    print("🔄 正在检查并初始化数据库...")
    async with aiosqlite.connect(DB_NAME) as db:
        # 1. 保护贴主表
        await db.execute("""
            CREATE TABLE IF NOT EXISTS protected_items (
                message_id INTEGER PRIMARY KEY,
                channel_id INTEGER,
                owner_id INTEGER,
                unlock_type TEXT,
                storage_urls TEXT,
                title TEXT,
                log TEXT,
                password TEXT,
                created_at TEXT,
                download_count INTEGER DEFAULT 0
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
                log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                title TEXT,
                filenames TEXT,
                timestamp TEXT NOT NULL
            )
        """)

        # 5. 溯源追踪记录表
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

        # 6. 自动置底任务配置表 (新)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bump_config (
                channel_id INTEGER PRIMARY KEY
            )
        """)

        # 7. 贴主附件汇总置底消息记录表 (新)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sticky_messages (
                channel_id INTEGER PRIMARY KEY,
                message_id INTEGER NOT NULL
            )
        """)

        await db.commit()
    print("✅ 数据库初始化完成，所有表结构已就绪。")


def get_db():
    """获取数据库连接对象"""
    return aiosqlite.connect(DB_NAME)