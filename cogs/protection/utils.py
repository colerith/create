# protection/utils.py

import re
import json
import asyncio
import io
import discord
import aiosqlite
import struct
import binascii
import os
import uuid
import random
import zipfile

from datetime import datetime
from ..core.db import get_db
from config import TZ_SHANGHAI, DAILY_DOWNLOAD_LIMIT, TEST_ROLE_ID

# --- 魔法签名与隐写配置 ---
# 格式: 0x00 + NOVA_TRACE: + UUID(12)
MAGIC_HEADER = b'\x00NOVA_TRACE:'
ZIP_COMMENT_PREFIX = b'NOVA_TRACE:'
ZIP_MANIFEST_NAME = '.nova_trace.json'
ZIP_WRITE_MANIFEST = False
ZIP_SCAN_MAX_FILES = 200
ZIP_SCAN_MAX_TOTAL_BYTES = 32 * 1024 * 1024

# 零宽字符映射表：用于将二进制 0/1 转换为不可见字符
# \u200b (Zero Width Space) -> 0
# \u200c (Zero Width Non-Joiner) -> 1
ZW_ZERO = '\u200b'
ZW_ONE  = '\u200c'

STEALTH_KEY_CANDIDATES = [
    "paletteSeed",
    "renderProfile",
    "uiBlueprint",
    "displayLocale",
    "dynamicLayout",
    "stylePreset",
    "themeProfile",
]

STEALTH_VALUE_TOKENS = [
    "synced",
    "stable",
    "release",
    "cached",
    "preview",
    "verified",
]

def generate_trace_id():
    """生成12位短ID"""
    return uuid.uuid4().hex[:12]

# --- 零宽字符隐写工具函数 ---
def text_to_zw(text: str) -> str:
    """将普通文本转换为零宽字符序列"""
    binary = ''.join(format(ord(c), '08b') for c in text)
    return ''.join(ZW_ZERO if b == '0' else ZW_ONE for b in binary)

def zw_to_text(zw_string: str) -> str:
    """提取并在零宽字符序列中解码出原文本"""
    # 过滤掉非零宽字符
    filtered = [c for c in zw_string if c in (ZW_ZERO, ZW_ONE)]
    if not filtered: return None

    binary_str = ''.join('0' if c == ZW_ZERO else '1' for c in filtered)

    # 每8位转回一个字符
    chars = []
    for i in range(0, len(binary_str), 8):
        byte = binary_str[i:i+8]
        if len(byte) < 8: break
        chars.append(chr(int(byte, 2)))
    return ''.join(chars)

def _inject_png_text_chunk(data, key, text):
    """
    专门为 PNG 注入 tEXt 块。
    """
    if data[:8] != b'\x89PNG\r\n\x1a\n': return data

    raw_data = key.encode('latin-1') + b'\x00' + text.encode('latin-1')
    length = len(raw_data)
    chunk_type = b'tEXt'
    crc = binascii.crc32(chunk_type)
    crc = binascii.crc32(raw_data, crc) & 0xffffffff
    chunk = struct.pack('!I', length) + chunk_type + raw_data + struct.pack('!I', crc)

    iend_pos = data.rfind(b'\x00\x00\x00\x00IEND\xaeB`\x82')
    if iend_pos == -1: return data + chunk
    return data[:iend_pos] + chunk + data[iend_pos:]

def _inject_zip_trace(file_bytes, trace_id):
    """
    ZIP 溯源增强：
    1) 写入 zip comment（包级标记）
    2) 写入隐藏 manifest（便于直接溯源压缩包）
    3) 对每个普通文件做二次注入（便于解压后单文件溯源）
    """
    try:
        src = io.BytesIO(file_bytes)
        out = io.BytesIO()

        with zipfile.ZipFile(src, 'r') as zin, zipfile.ZipFile(out, 'w') as zout:
            infos = zin.infolist()
            for info in infos:
                raw = zin.read(info.filename)

                if info.is_dir():
                    zout.writestr(info, raw)
                    continue

                base_name = os.path.basename(info.filename)
                if base_name == ZIP_MANIFEST_NAME:
                    continue

                new_raw = inject_smart_trace(raw, base_name, trace_id)
                zout.writestr(info, new_raw)

            if ZIP_WRITE_MANIFEST:
                manifest = {
                    "trace_id": trace_id,
                    "issuer": "ProtectionBot",
                    "version": 1
                }
                zout.writestr(ZIP_MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False))
            zout.comment = ZIP_COMMENT_PREFIX + trace_id.encode('ascii')

        return out.getvalue()
    except Exception as e:
        print(f"ZIP Injection Error: {e}")
        return file_bytes + MAGIC_HEADER + trace_id.encode('ascii')

def _extract_trace_from_zip_bytes(file_bytes):
    """从 ZIP 本体或 ZIP 内文件中提取 TraceID。"""
    try:
        total_scanned = 0
        with zipfile.ZipFile(io.BytesIO(file_bytes), 'r') as zf:
            comment = zf.comment or b''
            if comment.startswith(ZIP_COMMENT_PREFIX):
                tid = comment[len(ZIP_COMMENT_PREFIX):len(ZIP_COMMENT_PREFIX)+12].decode('ascii', errors='ignore')
                if re.fullmatch(r'[a-f0-9]{12}', tid):
                    return tid

            if ZIP_MANIFEST_NAME in zf.namelist():
                try:
                    manifest_raw = zf.read(ZIP_MANIFEST_NAME)
                    manifest = json.loads(manifest_raw.decode('utf-8', errors='ignore'))
                    tid = str(manifest.get('trace_id', ''))
                    if re.fullmatch(r'[a-f0-9]{12}', tid):
                        return tid
                except Exception:
                    pass

            for i, info in enumerate(zf.infolist()):
                if i >= ZIP_SCAN_MAX_FILES:
                    break
                if info.is_dir():
                    continue
                if info.filename.endswith('/') or os.path.basename(info.filename) == ZIP_MANIFEST_NAME:
                    continue
                if info.file_size <= 0:
                    continue
                if total_scanned > ZIP_SCAN_MAX_TOTAL_BYTES:
                    break

                raw = zf.read(info.filename)
                total_scanned += len(raw)
                inner_name = os.path.basename(info.filename)
                inner_tid = extract_trace_from_bytes(raw, inner_name)
                if inner_tid and re.fullmatch(r'[a-f0-9]{12}', inner_tid):
                    return inner_tid
    except Exception:
        return None

    return None

def inject_smart_trace(file_bytes, filename, trace_id):
    """
    智能注入溯源信息 (升级版：支持隐形水印)
    """
    ext = os.path.splitext(filename)[1].lower()

    try:
        # 策略 1: PNG 隐写
        if ext == '.zip':
            print(f"Injecting ZIP Trace: {trace_id}")
            return _inject_zip_trace(file_bytes, trace_id)
        if ext == '.png':
            print(f"Injecting PNG Trace: {trace_id}")
            return _inject_png_text_chunk(file_bytes, "Software", f"ProtectionBot | ID:{trace_id}")

        # 策略 2: JSON 高级隐写
        elif ext == '.json':
            try:
                content = file_bytes.decode('utf-8')
                json_obj = json.loads(content)

                if isinstance(json_obj, dict):
                    # 新策略：新增一个伪装字段承载隐形水印，避免污染现有字段
                    hidden_mark = text_to_zw(f"TRACE:{trace_id}")

                    base_keys = STEALTH_KEY_CANDIDATES or ["uiProfile", "layoutMatrix", "styleMatrix"]
                    stealth_key = random.choice(base_keys)
                    attempts = 0
                    while stealth_key in json_obj and attempts < 6:
                        stealth_key = f"{random.choice(base_keys)}{random.randint(10,99)}"
                        attempts += 1
                    if stealth_key in json_obj:
                        stealth_key = f"traceMeta_{trace_id[:4]}_{random.randint(100,999)}"

                    value_tokens = STEALTH_VALUE_TOKENS or ["stable", "synced", "release"]
                    stealth_value = f"{random.choice(value_tokens)}-{random.randint(100,999)}{hidden_mark}"

                    items = list(json_obj.items())
                    insert_idx = random.randint(0, len(items))
                    items.insert(insert_idx, (stealth_key, stealth_value))
                    json_obj = {k: v for k, v in items}

                    # B. 伪装字段 (明面上的诱饵)
                    fake_hash = uuid.uuid4().hex
                    json_obj['_integrity_check'] = f"{fake_hash}.{trace_id[:4]}"

                    return json.dumps(json_obj, indent=2, ensure_ascii=False).encode('utf-8')
            except:
                pass # JSON解析失败，回退

        # 策略 3: 通用二进制追加 (保底方案)
        # 直接在末尾追加二进制水印
        trace_payload = MAGIC_HEADER + trace_id.encode('ascii')
        return file_bytes + trace_payload

    except Exception as e:
        print(f"Injection Error: {e}")
        return file_bytes

def extract_trace_from_bytes(file_bytes, filename):
    """
    尝试从文件数据中提取 TraceID (升级版：支持读取隐形水印)
    返回: trace_id_str 或 None
    """
    try:
        ext = os.path.splitext(filename)[1].lower()
        trace_id = None
        if ext == '.zip':
            zip_tid = _extract_trace_from_zip_bytes(file_bytes)
            if zip_tid:
                return zip_tid

        # 1. 优先检查 JSON 隐形水印 (最高优先级)
        if ext == '.json':
            try:
                content = file_bytes.decode('utf-8')

                decoded_text = zw_to_text(content)
                if decoded_text and "TRACE:" in decoded_text:
                    # 提取 TRACE: 后面的部分
                    match = re.search(r'TRACE:([a-f0-9]{12})', decoded_text)
                    if match:
                        return match.group(1)

                json_obj = json.loads(content)
                if isinstance(json_obj, dict):
                    # 检查旧版字段
                    if '_protection_trace' in json_obj:
                         return json_obj['_protection_trace'].get('id')
                    if '_integrity_check' in json_obj:
                        val = json_obj['_integrity_check']
                        if '.' in val:
                            return val.split('.')[-1] # 只拿后缀
            except:
                pass

        # 2. 检查 PNG tEXt 块
        s_bytes = file_bytes
        header_pattern = b'ProtectionBot | ID:'
        idx = s_bytes.find(header_pattern)
        if idx != -1:
             start = idx + len(header_pattern)
             return s_bytes[start:start+12].decode('ascii', errors='ignore')

        # 3. 检查通用追加尾巴 (保底)
        pos = file_bytes.rfind(MAGIC_HEADER)
        if pos != -1:
            start = pos + len(MAGIC_HEADER)
            trace_id = file_bytes[start:start+12].decode('ascii', errors='ignore')
            return trace_id

        return None
    except Exception as e:
        print(f"Extraction Error: {e}")
        return None

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
