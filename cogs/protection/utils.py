import re
import json
import asyncio
import io
import discord
import aiosqlite
from datetime import datetime
from database import get_db
from .constants import TZ_SHANGHAI, DAILY_DOWNLOAD_LIMIT, TEST_ROLE_ID

# --- 评论验证 ---
def is_valid_comment(content: str) -> bool:
    if not content: return False
    content_no_emoji = re.sub(r'<a?:.+?:\d+>', '', content)
    content_clean = re.sub(r'http\S+', '', content_no_emoji).strip()
    content_clean = re.sub(r'\s+', '', content_clean) 
    if len(content_clean) <= 5: return False
    if content_clean.isdigit(): return False
    if re.search(r'(.)\1{4,}', content_clean): return False
    if len(set(content_clean)) < 4: return False
    return True

# --- 通用下载逻辑 ---
async def fetch_files_common(bot, file_data):
    results = []
    if not isinstance(file_data, list): return []
    fetched_messages = {} 

    for item in file_data:
        if not isinstance(item, dict): continue
        download_url = item.get('url')
        
        if item.get('strategy') == 'msg_ref':
            cid = item.get('channel_id')
            mid = item.get('message_id')
            idx = item.get('attachment_index', 0)
            
            if cid and mid:
                msg = fetched_messages.get((cid, mid))
                if not msg:
                    try:
                        channel = bot.get_channel(cid)
                        if not channel: 
                            try: channel = await bot.fetch_channel(cid)
                            except: pass 
                        
                        if channel:
                            msg = await channel.fetch_message(mid)
                            fetched_messages[(cid, mid)] = msg
                    except Exception:
                        pass
                
                if msg and 0 <= idx < len(msg.attachments):
                    download_url = msg.attachments[idx].url

        if not download_url: continue

        try:
            async with bot.http_session.get(download_url) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    if len(data) > 0:
                        results.append({'filename': item.get('filename', 'unknown'), 'bytes': data})
        except Exception as e: 
            print(f"DL Error: {e}")
            
    return results

def make_discord_files_common(file_results):
    return [discord.File(io.BytesIO(res['bytes']), filename=res['filename']) for res in file_results]

async def record_download_common(user, item_row):
    async def _update():
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            message_id = item_row['message_id']
            await db.execute("UPDATE protected_items SET download_count = download_count + 1 WHERE message_id = ?", (message_id,))
            try:
                file_data = json.loads(item_row['storage_urls'])
                filenames = json.dumps([f.get('filename','unknown') for f in file_data if isinstance(f, dict)])
            except: filenames = "[]"
            # 确保 download_log 表存在
            await db.execute("INSERT INTO download_log (user_id, message_id, title, filenames, timestamp) VALUES (?, ?, ?, ?, ?)",
                           (user.id, message_id, item_row['title'], filenames, datetime.now(TZ_SHANGHAI).isoformat()))
            await db.commit()
    asyncio.create_task(_update())

# --- 核心鉴权逻辑 ---
async def check_requirements_common(interaction, unlock_type, owner_id, panel_message_id):
    user = interaction.user
    
    # 1. 身份特权
    has_test_role = isinstance(user, discord.Member) and user.get_role(TEST_ROLE_ID)
    is_owner = (user.id == owner_id)
    if is_owner and has_test_role: is_owner = False 
    if is_owner: return True, "owner"

    # 2. 频率限制
    today_start = datetime.now(TZ_SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    async with get_db() as db:
        cursor = await db.execute("SELECT COUNT(*) FROM download_log WHERE user_id = ? AND timestamp >= ?", (user.id, today_start))
        cnt = (await cursor.fetchone())[0]
    if cnt >= DAILY_DOWNLOAD_LIMIT:
        return False, f"⚠️ 您今日的下载次数已达上限（{DAILY_DOWNLOAD_LIMIT}/{DAILY_DOWNLOAD_LIMIT}）。"

    # 3. 点赞检测
    target_check_id = panel_message_id
    if isinstance(interaction.channel, discord.Thread):
        target_check_id = interaction.channel.id 

    has_liked = False
    async with get_db() as db:
        cursor = await db.execute("SELECT 1 FROM user_likes WHERE user_id = ? AND message_id = ?", (user.id, target_check_id))
        if await cursor.fetchone(): has_liked = True
        
        if not has_liked:
            cursor = await db.execute("SELECT 1 FROM cached_likes WHERE user_id = ? AND message_id = ?", (user.id, target_check_id))
            if await cursor.fetchone(): has_liked = True

    if not has_liked:
        jump_url = f"https://discord.com/channels/{interaction.guild_id}/{interaction.channel_id}/{target_check_id}"
        return False, (
            f"🛑 **数据库未找到点赞记录！**\n"
            f"请跳转到 **[帖子首楼]({jump_url})** 点个赞 👍。\n"
            f"⚠️ **重要提示**：Bot不实时监听旧消息，如果没反应，**请取消点赞，然后重新点一次**，即可秒级记录。"
        )

    # 4. 评论检测
    if "comment" in unlock_type:
        has_commented = False
        current_thread_id = interaction.channel.id
        
        async with get_db() as db:
            cursor = await db.execute("SELECT 1 FROM user_comments WHERE user_id = ? AND message_id = ?", (user.id, current_thread_id))
            if await cursor.fetchone(): has_commented = True
        
        if not has_commented:
            return False, (
                "💬 **数据库未找到评论记录！**\n"
                "请在当前帖子内发送一条有意义的评论（>5字，禁纯水）。\n"
                "⚠️ **注意**：Bot 不会读取历史消息。如果您之前评论过但没被记录，请**重新发一条**。"
            )

    return True, "passed"
