# cogs/core/db.py

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
                update_log TEXT,
                mention_users INTEGER DEFAULT 0,
                password TEXT,
                created_at TEXT,
                download_count INTEGER DEFAULT 0
            )
        """)
        # 1.1 兼容旧表结构：补充更新日志与艾特配置列
        try:
            await db.execute("ALTER TABLE protected_items ADD COLUMN update_log TEXT")
        except:
            pass
        try:
            await db.execute(
                "ALTER TABLE protected_items ADD COLUMN mention_users INTEGER DEFAULT 0"
            )
        except:
            pass
        try:
            await db.execute("ALTER TABLE protected_items ADD COLUMN file_meta TEXT")
        except:
            pass

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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS download_rate_warning_log (
                warning_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                message_id INTEGER,
                title TEXT,
                warning_type TEXT NOT NULL DEFAULT 'rate_limit',
                timestamp TEXT NOT NULL
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_download_rate_warning_user_ts ON download_rate_warning_log (user_id, timestamp)"
        )
        # 4.1 发布保护附件时的更新日志记录表
        await db.execute("""
            CREATE TABLE IF NOT EXISTS attachment_update_publish_log (
                log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                guild_id INTEGER,
                channel_id INTEGER NOT NULL,
                protected_message_id INTEGER NOT NULL,
                update_message_id INTEGER NOT NULL,
                title TEXT,
                update_log TEXT NOT NULL,
                mention_users INTEGER NOT NULL DEFAULT 0,
                timestamp TEXT NOT NULL
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_attachment_update_publish_owner_ts ON attachment_update_publish_log (owner_id, timestamp)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_attachment_update_publish_channel_ts ON attachment_update_publish_log (channel_id, timestamp)"
        )
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
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_file_traces_user ON file_traces (user_id)"
        )
        # 6. 自动置底任务配置表
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bump_config (
                channel_id INTEGER PRIMARY KEY
            )
        """)
        # 7. 贴主附件汇总置底消息记录表
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sticky_messages (
                channel_id INTEGER PRIMARY KEY,
                message_id INTEGER NOT NULL
            )
        """)

        # --- 【新增】第8步：持久化面板记录表 ---
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
        # 为 panel_type 创建索引可以加快查询速度
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_panel_type ON panel_records (panel_type)"
        )

        # 9. 探索面板推送记录：用于定时编辑刷新 DM/频道里的旧面板
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
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_exploration_panel_pushes_refresh ON exploration_panel_pushes (panel_type, delivery_type)"
        )

        await db.commit()
    print("✅ 数据库初始化完成，所有表结构已就绪。")


def get_db():
    """获取数据库连接对象"""
    return aiosqlite.connect(DB_NAME)


# === 面板记录辅助函数 ===


async def get_panel_message_id(
    channel_id: int, panel_type: str = "daily_report"
) -> int | None:
    """根据频道ID和面板类型获取消息ID"""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT message_id FROM panel_records WHERE channel_id = ? AND panel_type = ?",
            (channel_id, panel_type),
        )
        row = await cursor.fetchone()
        return row[0] if row else None


async def set_panel_message_id(
    channel_id: int, message_id: int, panel_type: str = "daily_report"
):
    """设置或更新面板的消息ID"""
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO panel_records (channel_id, panel_type, message_id)
            VALUES (?, ?, ?)
            ON CONFLICT(channel_id, panel_type) DO UPDATE SET message_id = excluded.message_id
            """,
            (channel_id, panel_type, message_id),
        )
        await db.commit()


async def remove_panel_record(channel_id: int, panel_type: str = "daily_report"):
    """移除面板记录"""
    async with get_db() as db:
        await db.execute(
            "DELETE FROM panel_records WHERE channel_id = ? AND panel_type = ?",
            (channel_id, panel_type),
        )
        await db.commit()
