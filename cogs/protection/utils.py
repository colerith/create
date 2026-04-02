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

MAGIC_HEADER = b'\x00NOVA_TRACE:'
ZIP_COMMENT_PREFIX = b'NOVA_TRACE:'
ZIP_MANIFEST_NAME = '.nova_trace.json'
ZIP_WRITE_MANIFEST = False
ZIP_SCAN_MAX_FILES = 200
ZIP_SCAN_MAX_TOTAL_BYTES = 32 * 1024 * 1024
JSON_WS_PREFIX = "\n\t \t\t  "
JSON_WS_SUFFIX = " \t\t \n"


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
    return uuid.uuid4().hex[:12]

def text_to_zw(text: str) -> str:
    binary = ''.join(format(ord(c), '08b') for c in text)
    return ''.join(ZW_ZERO if b == '0' else ZW_ONE for b in binary)

def zw_to_text(zw_string: str) -> str:
    """Decode hidden zero-width text payload."""
    filtered = [c for c in zw_string if c in (ZW_ZERO, ZW_ONE)]
    if not filtered: return None

    binary_str = ''.join('0' if c == ZW_ZERO else '1' for c in filtered)

    chars = []
    for i in range(0, len(binary_str), 8):
        byte = binary_str[i:i+8]
        if len(byte) < 8: break
        chars.append(chr(int(byte, 2)))
    return ''.join(chars)

def _encode_trace_to_ws(trace_id: str) -> bytes:
    """Encode trace_id into trailing JSON whitespace (space/tab)."""
    bits = ''.join(format(ord(c), '08b') for c in trace_id)
    payload = ''.join(' ' if b == '0' else '\t' for b in bits)
    return (JSON_WS_PREFIX + payload + JSON_WS_SUFFIX).encode('ascii')

def _extract_trace_from_json_whitespace(file_bytes: bytes):
    """Extract trace_id from trailing JSON whitespace watermark."""
    try:
        text = file_bytes.decode('utf-8', errors='ignore')
        idx = text.rfind(JSON_WS_PREFIX)
        if idx == -1:
            return None

        start = idx + len(JSON_WS_PREFIX)
        bit_len = 12 * 8
        bits_ws = text[start:start + bit_len]
        suffix = text[start + bit_len:start + bit_len + len(JSON_WS_SUFFIX)]
        if len(bits_ws) != bit_len or suffix != JSON_WS_SUFFIX:
            return None
        if any(ch not in (' ', '\t') for ch in bits_ws):
            return None

        bits = ''.join('0' if ch == ' ' else '1' for ch in bits_ws)
        chars = []
        for i in range(0, len(bits), 8):
            chars.append(chr(int(bits[i:i+8], 2)))
        tid = ''.join(chars)
        if re.fullmatch(r'[a-f0-9]{12}', tid):
            return tid
    except Exception:
        return None
    return None

def _inject_png_text_chunk(data, key, text):
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
    """Extract trace_id from zip comment/manifest/inner files."""
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
    ext = os.path.splitext(filename)[1].lower()

    try:
        if ext == '.zip':
            print(f"Injecting ZIP Trace: {trace_id}")
            return _inject_zip_trace(file_bytes, trace_id)
        if ext == '.png':
            print(f"Injecting PNG Trace: {trace_id}")
            return _inject_png_text_chunk(file_bytes, "Software", f"ProtectionBot | ID:{trace_id}")

        elif ext == '.json':
            # 结构无损：仅在文件末尾追加合法空白字符（space/tab/newline）
            # 不 parse/dump JSON，避免字段重排和 schema 破坏
            return file_bytes + _encode_trace_to_ws(trace_id)

        trace_payload = MAGIC_HEADER + trace_id.encode('ascii')
        return file_bytes + trace_payload

    except Exception as e:
        print(f"Injection Error: {e}")
        return file_bytes

def extract_trace_from_bytes(file_bytes, filename):
    try:
        ext = os.path.splitext(filename)[1].lower()
        trace_id = None
        if ext == '.zip':
            zip_tid = _extract_trace_from_zip_bytes(file_bytes)
            if zip_tid:
                return zip_tid

        if ext == '.json':
            try:
                ws_tid = _extract_trace_from_json_whitespace(file_bytes)
                if ws_tid:
                    return ws_tid

                content = file_bytes.decode('utf-8')

                decoded_text = zw_to_text(content)
                if decoded_text and "TRACE:" in decoded_text:
                    match = re.search(r'TRACE:([a-f0-9]{12})', decoded_text)
                    if match:
                        return match.group(1)

                json_obj = json.loads(content)
                if isinstance(json_obj, dict):
                    if '_protection_trace' in json_obj:
                         return json_obj['_protection_trace'].get('id')
                    if '_integrity_check' in json_obj:
                        val = json_obj['_integrity_check']
                        if '.' in val:
                            return val.split('.')[-1]
            except:
                pass

        s_bytes = file_bytes
        header_pattern = b'ProtectionBot | ID:'
        idx = s_bytes.find(header_pattern)
        if idx != -1:
             start = idx + len(header_pattern)
             return s_bytes[start:start+12].decode('ascii', errors='ignore')

        pos = file_bytes.rfind(MAGIC_HEADER)
        if pos != -1:
            start = pos + len(MAGIC_HEADER)
            trace_id = file_bytes[start:start+12].decode('ascii', errors='ignore')
            return trace_id

        return None
    except Exception as e:
        print(f"Extraction Error: {e}")
        return None

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
            await db.execute("INSERT INTO download_log (user_id, message_id, title, filenames, timestamp) VALUES (?, ?, ?, ?, ?)",
                           (user.id, message_id, item_row['title'], filenames, datetime.now(TZ_SHANGHAI).isoformat()))
            await db.commit()
    asyncio.create_task(_update())

async def check_requirements_common(interaction, unlock_type, owner_id, panel_message_id):
    user = interaction.user
    
    has_test_role = isinstance(user, discord.Member) and user.get_role(TEST_ROLE_ID)
    is_owner = (user.id == owner_id)
    if is_owner and has_test_role: is_owner = False 
    if is_owner: return True, "owner"

    today_start = datetime.now(TZ_SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    async with get_db() as db:
        cursor = await db.execute("SELECT COUNT(*) FROM download_log WHERE user_id = ? AND timestamp >= ?", (user.id, today_start))
        cnt = (await cursor.fetchone())[0]
    if cnt >= DAILY_DOWNLOAD_LIMIT:
        return False, f"⚠️ 您今日下载次数已达上限（{DAILY_DOWNLOAD_LIMIT}/{DAILY_DOWNLOAD_LIMIT}）。"

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
            f"🛰️ **数据库未找到点赞记录**\n"
            f"请先前往 **[帖子首楼]({jump_url})** 点个赞 👍\n"
            f"⚠️ **提示**：如果没触发记录，请取消点赞后再点一次。"
        )

    if "comment" in unlock_type:
        has_commented = False
        current_thread_id = interaction.channel.id
        
        async with get_db() as db:
            cursor = await db.execute("SELECT 1 FROM user_comments WHERE user_id = ? AND message_id = ?", (user.id, current_thread_id))
            if await cursor.fetchone(): has_commented = True
        
        if not has_commented:
            return False, (
                "💬 **数据库未找到评论记录**\n"
                "请在当前帖子内发送一条有意义的评论（>5字）。\n"
                "⚠️ **提示**：机器人不会回溯历史消息，若此前评论未记录，请再发一条。"
            )

    return True, "passed"
