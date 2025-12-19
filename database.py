import aiosqlite

DB_NAME = "chimidan.db"

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        
        await db.execute("""
            CREATE TABLE IF NOT EXISTS protected_items (
                message_id INTEGER PRIMARY KEY, channel_id INTEGER, owner_id INTEGER,
                unlock_type TEXT, storage_urls TEXT, title TEXT, log TEXT, password TEXT,
                created_at TEXT, download_count INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_unlocks (
                user_id INTEGER, message_id INTEGER, comment TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (user_id, message_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS download_log (
                log_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                title TEXT, filenames TEXT, timestamp TEXT NOT NULL
            )
        """)
        
        try:
            await db.execute("ALTER TABLE protected_items ADD COLUMN created_at TEXT")
        except:
            pass 
        await db.commit()

def get_db():
    return aiosqlite.connect(DB_NAME)