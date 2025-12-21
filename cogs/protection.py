# protection.py

import discord
from discord import app_commands, ui
from discord.ext import commands, tasks
import json
import asyncio
import io
import re
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import quote
import aiosqlite

from database import get_db

TZ_SHANGHAI = ZoneInfo("Asia/Shanghai")
DAILY_DOWNLOAD_LIMIT = 50
TEST_ROLE_ID = 1402290127627091979

# --- Helper: Comment Validator ---
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

# --- Shared Logic Helpers ---

async def fetch_files_common(bot, file_data):
    """通用文件下载逻辑"""
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
                        if not channel: channel = await bot.fetch_channel(cid)
                        msg = await channel.fetch_message(mid)
                        fetched_messages[(cid, mid)] = msg
                    except Exception as e:
                        print(f"Failed to refresh URL ref: {e}")
                
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
            await db.execute("INSERT INTO download_log (user_id, message_id, title, filenames, timestamp) VALUES (?, ?, ?, ?, ?)", (user.id, message_id, item_row['title'], filenames, datetime.now(TZ_SHANGHAI).isoformat())); await db.commit()
    asyncio.create_task(_update())

# --- 核心验证逻辑 (已修正首楼定位) ---

async def check_requirements_common(interaction, unlock_type, owner_id, target_message_id):
    """
    通用验证逻辑 (数据库优先 + 首楼修正版)
    target_message_id: 这是 protected_items 表里的 message_id (即 Bot 面板消息ID)
    """
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
        if (await cursor.fetchone())[0] >= DAILY_DOWNLOAD_LIMIT:
            return False, f"⚠️ 您今日的下载次数已达上限（{DAILY_DOWNLOAD_LIMIT}/{DAILY_DOWNLOAD_LIMIT}）。"

    # === 验证点赞 ===
    has_liked = False
    
    # === 验证点赞 ===
    has_liked = False
    
    # A. 优先查本地数据库
    async with get_db() as db:
        cursor = await db.execute("SELECT 1 FROM user_likes WHERE user_id = ? AND message_id = ?", (user.id, target_message_id))
        if await cursor.fetchone():
            has_liked = True

    # B. 数据库没查到？回退到 API 检查 (防429优化)
    if not has_liked:
        try:
            op_msg = None
            if isinstance(interaction.channel, discord.Thread):
                op_msg = interaction.channel.starter_message
                if not op_msg:
                    try: op_msg = await interaction.channel.fetch_message(interaction.channel.id)
                    except discord.NotFound: op_msg = await interaction.channel.fetch_message(target_message_id)
            else:
                op_msg = await interaction.channel.fetch_message(target_message_id)

            if op_msg:
                # 【优化1】按点赞数量从多到少排序，优先检查热门表情
                # 这样大概率在第一次循环就能找到用户，避免后续的请求
                sorted_reactions = sorted(op_msg.reactions, key=lambda r: r.count, reverse=True)
                
                # 【优化2】限制最多只检查前 5 种热门表情 (防止有人恶意刷几十种冷门表情炸Bot)
                for r in sorted_reactions[:5]: 
                    if r.count == 0: continue
                    
                    # 批量获取前 100 个用户并缓存
                    users = []
                    async for u in r.users(limit=100): 
                        users.append(u)
                    
                    if users:
                        async with get_db() as db:
                            for u in users:
                                await db.execute(
                                    "INSERT OR IGNORE INTO user_likes (user_id, message_id) VALUES (?, ?)", 
                                    (u.id, target_message_id)
                                )
                            await db.commit()

                    if any(u.id == user.id for u in users):
                        has_liked = True
                        break 
                    
                    # 【核心修改】将休息时间从 0.5 改为 2.0 秒
                    # 这是为了给 API 喘息的机会
                    await asyncio.sleep(2.0)

        except Exception as e:
            print(f"API Fallback Check Error: {e}")

    if not has_liked:
        # 生成跳转链接指向首楼
        thread_jump_url = interaction.channel.jump_url if isinstance(interaction.channel, discord.Thread) else f"https://discord.com/channels/{interaction.guild_id}/{interaction.channel_id}/{target_message_id}"
        return False, f"🛑 您还没点赞呢！\n请跳转到 **[帖子首楼]({thread_jump_url})** 点个赞吧！👍\n*(如果是刚才点的，请等待几秒后再试)*"

    # === 验证评论 ===
    if "comment" in unlock_type:
        has_commented = False
        
        # A. 优先查本地数据库
        async with get_db() as db:
            cursor = await db.execute("SELECT 1 FROM user_comments WHERE user_id = ? AND message_id = ?", (user.id, target_message_id))
            if await cursor.fetchone():
                has_commented = True
        
        # B. 回退查 API (只查最近的历史消息)
        if not has_commented:
            try:
                # 限制 limit 防止 429
                async for msg in interaction.channel.history(limit=50): 
                    if msg.author.id == user.id and is_valid_comment(msg.content):
                        has_commented = True
                        # 补录
                        async with get_db() as db:
                            await db.execute("INSERT OR REPLACE INTO user_comments (user_id, message_id, content) VALUES (?, ?, ?)", (user.id, target_message_id, "History Check"))
                            await db.commit()
                        break
            except: pass

        if not has_commented:
            return False, "💬 **评论未达标！**\n请在 **本下载面板下方** 发送一条有意义的评论（>5字，禁纯水）。"

    return True, "passed"

# --- Modal Classes ---

class DraftTitleModal(ui.Modal, title="设置标题"):
    title_input = ui.TextInput(label="标题", placeholder="请输入...", max_length=100)
    def __init__(self, view): super().__init__(); self.view_ref = view; self.title_input.default = view.draft_title
    async def on_submit(self, i: discord.Interaction): self.view_ref.draft_title = i.data['components'][0]['components'][0]['value']; await self.view_ref.update_dashboard(i)

class DraftNoteModal(ui.Modal, title="设置作者提示"):
    log_input = ui.TextInput(label="说明/日志", style=discord.TextStyle.paragraph, placeholder="写点什么...", max_length=4000, required=False)
    def __init__(self, view): super().__init__(); self.view_ref = view; self.log_input.default = view.draft_log[:4000] if view.draft_log else None
    async def on_submit(self, i: discord.Interaction): self.view_ref.draft_log = i.data['components'][0]['components'][0]['value']; await self.view_ref.update_dashboard(i)

class DraftPasswordModal(ui.Modal, title="设置口令"):
    pwd_input = ui.TextInput(label="下载口令", placeholder="1-100字", min_length=1, max_length=100)
    def __init__(self, view, next_mode): super().__init__(); self.view_ref = view; self.next_mode = next_mode; self.pwd_input.default = view.draft_password
    async def on_submit(self, i: discord.Interaction):
        clean_pwd = i.data['components'][0]['components'][0]['value'].strip()
        if not clean_pwd: return await i.response.send_message("口令不能为空！", ephemeral=True)
        self.view_ref.draft_password = clean_pwd; self.view_ref.draft_mode = self.next_mode; await self.view_ref.update_dashboard(i)

# --- Renaming Logic ---

class RenameFileModal(ui.Modal, title="重命名文件"):
    name_input = ui.TextInput(label="新文件名 (无需输入后缀)", placeholder="例如：我的汉化补丁", max_length=100)
    def __init__(self, view_ref, file_index, old_filename):
        super().__init__()
        self.view_ref = view_ref
        self.file_index = file_index
        self.name_stem, self.ext = os.path.splitext(old_filename)
        self.name_input.default = self.name_stem
    async def on_submit(self, interaction: discord.Interaction):
        new_stem = self.name_input.value.strip()
        if not new_stem: return await interaction.response.send_message("文件名不能为空！", ephemeral=True)
        new_full_name = f"{new_stem}{self.ext}"
        self.view_ref.custom_names[self.file_index] = new_full_name
        await interaction.response.defer(ephemeral=True)
        await self.view_ref.update_dashboard(interaction)
        await interaction.followup.send(f"✅ 文件已重命名为：`{new_full_name}`", ephemeral=True)

class FileSelectView(ui.View):
    def __init__(self, protection_view):
        super().__init__(timeout=60)
        self.protection_view = protection_view
        options = []
        for i, att in enumerate(protection_view.attachments):
            current_name = protection_view.custom_names.get(i, att.filename)
            label = current_name[:95]
            options.append(discord.SelectOption(label=f"{i+1}. {label}", value=str(i), description=f"原始: {att.filename[:50]}"))
        self.select_menu = ui.Select(placeholder="选择要改名的文件...", options=options, min_values=1, max_values=1)
        self.select_menu.callback = self.select_callback
        self.add_item(self.select_menu)
    async def select_callback(self, interaction: discord.Interaction):
        idx = int(self.select_menu.values[0])
        current_name = self.protection_view.custom_names.get(idx, self.protection_view.attachments[idx].filename)
        await interaction.response.send_modal(RenameFileModal(self.protection_view, idx, current_name))

# --- Creator View (Draft) ---

class ProtectionDraftView(ui.View):
    def __init__(self, bot, user, attachments, target_message=None, default_log=None):
        super().__init__(timeout=600)
        self.bot = bot
        self.user = user
        self.attachments = attachments
        self.target_message = target_message
        self.draft_title = f"{user.display_name} 的保护附件"
        self.draft_log = default_log
        self.draft_password = None
        self.draft_mode = "like"
        self.custom_names = {} 
    
    async def update_dashboard(self, interaction: discord.Interaction):
        log_preview = self.draft_log[:50] + "..." if self.draft_log and len(self.draft_log) > 50 else self.draft_log
        renamed_count = len(self.custom_names)
        file_status = f"{len(self.attachments)} 个"
        if renamed_count > 0: file_status += f" (已改名 {renamed_count} 个)"

        status_desc = (f"📦 **已传文件**: {file_status}\n🏷️ **当前标题**: {self.draft_title}\n📝 **作者提示**: {'✅ ' + log_preview if self.draft_log else '⚪ 未设置'}\n")
        mode_map = {"like": "👍 点赞解锁", "like_comment": "💬 点赞+评论", "like_password": f"🔐 点赞+口令 (口令: ||{self.draft_password}||)", "like_comment_password": f"🔐💬 点赞+评论+口令 (口令: ||{self.draft_password}||)"}
        status_desc += f"⚙️ **获取方式**: {mode_map.get(self.draft_mode)}"
        guide_desc = ("1️⃣ 点击 **第一排** 修改标题、说明或 **修改文件名**。\n2️⃣ 点击 **第二排** 选择解锁条件。\n3️⃣ 确认无误后，点击底部的 **🚀 确认发布**。")
        embed = discord.Embed(title="🛠️ 附件保护控制台", color=0x87ceeb); embed.add_field(name="📊 当前配置状态", value=status_desc, inline=False); embed.add_field(name="📖 操作指引", value=guide_desc, inline=False); embed.set_footer(text="此面板仅你自己可见")
        
        if interaction.response.is_done(): 
            try: await interaction.edit_original_response(content=None, embed=embed, view=self)
            except: pass
        else: await interaction.response.edit_message(content=None, embed=embed, view=self)

    @ui.button(label="修改标题", style=discord.ButtonStyle.secondary, row=0, emoji="🏷️")
    async def btn_set_title(self, i: discord.Interaction, b: ui.Button): await i.response.send_modal(DraftTitleModal(self))
    @ui.button(label="作者提示", style=discord.ButtonStyle.secondary, row=0, emoji="📝")
    async def btn_set_note(self, i: discord.Interaction, b: ui.Button): await i.response.send_modal(DraftNoteModal(self))
    @ui.button(label="改文件名", style=discord.ButtonStyle.secondary, row=0, emoji="✏️")
    async def btn_rename_files(self, i: discord.Interaction, b: ui.Button): await i.response.send_message("请选择要重命名的文件：", view=FileSelectView(self), ephemeral=True)
    @ui.button(label="查看文件", style=discord.ButtonStyle.secondary, row=0, emoji="📦")
    async def btn_view_files(self, i: discord.Interaction, b: ui.Button): 
        names = []
        for idx, att in enumerate(self.attachments):
            final_name = self.custom_names.get(idx, att.filename)
            names.append(f"{idx+1}. {final_name}")
        await i.response.send_message(f"**当前文件列表：**\n" + "\n".join(names)[:1900], ephemeral=True)
    
    @ui.button(label="点赞", style=discord.ButtonStyle.primary, row=1)
    async def mode_like(self, i: discord.Interaction, b: ui.Button): self.draft_mode = "like"; await self.update_dashboard(i)
    @ui.button(label="点赞+评论", style=discord.ButtonStyle.primary, row=1)
    async def mode_like_comment(self, i: discord.Interaction, b: ui.Button): self.draft_mode = "like_comment"; await self.update_dashboard(i)
    @ui.button(label="点赞+口令", style=discord.ButtonStyle.success, row=1, emoji="🔐")
    async def mode_like_pass(self, i: discord.Interaction, b: ui.Button): await i.response.send_modal(DraftPasswordModal(self, "like_password"))
    @ui.button(label="点赞+评论+口令", style=discord.ButtonStyle.success, row=1, emoji="🔐")
    async def mode_like_comm_pass(self, i: discord.Interaction, b: ui.Button): await i.response.send_modal(DraftPasswordModal(self, "like_comment_password"))
    
    @ui.button(label="确认发布", style=discord.ButtonStyle.danger, row=2, emoji="🚀")
    async def btn_confirm(self, i: discord.Interaction, b: ui.Button): 
        await i.response.edit_message(content="⏳ 正在加密上传...", embed=None, view=None)
        await self.publish(i)

    @ui.button(label="取消", style=discord.ButtonStyle.gray, row=2, emoji="✖️")
    async def btn_cancel(self, i: discord.Interaction, b: ui.Button): 
        await i.response.edit_message(content="操作已取消。", embed=None, view=None); self.stop()

    async def publish(self, interaction: discord.Interaction):
        files_to_send, file_metadata = [], []
        try:
            for idx, att in enumerate(self.attachments): 
                file_bytes = await att.read()
                final_filename = self.custom_names.get(idx, att.filename)
                f = discord.File(io.BytesIO(file_bytes), filename=final_filename)
                files_to_send.append(f)
        except Exception as e: return await interaction.followup.send(f"文件读取失败：{e}", ephemeral=True)
        
        stored_data = []
        try:
            dm = await self.user.create_dm()
            backup_msg = await dm.send(content=f"【{self.draft_title}】的备份！\nID: {interaction.id}", files=files_to_send)
            
            for i, att in enumerate(backup_msg.attachments):
                stored_data.append({
                    "strategy": "msg_ref", "channel_id": backup_msg.channel.id, "message_id": backup_msg.id,
                    "attachment_index": i, "filename": att.filename, "url": att.url
                })
        except discord.Forbidden: return await interaction.followup.send("无法私信备份（请开启私信权限）！", ephemeral=True)
        except Exception as e: return await interaction.followup.send(f"备份失败：{e}", ephemeral=True)

        if self.target_message:
            try: await self.target_message.delete()
            except: pass
            
        final_desc = self.draft_log if self.draft_log else "一份受保护的附件已发布，满足条件即可获取。"
        embed = discord.Embed(title=f"✨ {self.draft_title}", description=final_desc, color=discord.Color.from_rgb(255, 183, 197))
        embed.set_author(name=f"由 {self.user.display_name} 发布", icon_url=self.user.display_avatar.url)
        mode_map = {"like": "👍 **点赞首楼**", "like_comment": "👍💬 **点赞首楼 + 回复本贴**", "like_password": "👍🔐 **点赞首楼 + 口令**", "like_comment_password": "👍💬🔐 **点赞首楼 + 回复本贴 + 口令**"}
        embed.add_field(name="🔑 获取条件", value=mode_map.get(self.draft_mode, "未知"), inline=True)
        embed.add_field(name="📦 文件数量", value=f"**{len(stored_data)}** 个", inline=True)
        now_ts = discord.utils.format_dt(datetime.now(TZ_SHANGHAI))
        embed.add_field(name="⏰ 发布时间", value=now_ts, inline=True)
        embed.set_footer(text="由 创作保护助手 强力驱动", icon_url=self.bot.user.display_avatar.url)
        
        final_msg = await interaction.channel.send(embed=embed)
        try: await final_msg.pin(reason="附件保护自动标注")
        except: await interaction.followup.send("提示：我没有置顶权限！", ephemeral=True)
        
        async with get_db() as db:
            await db.execute(
                """INSERT INTO protected_items (message_id, channel_id, owner_id, unlock_type, storage_urls, title, log, password, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", 
                (final_msg.id, final_msg.channel.id, self.user.id, self.draft_mode, json.dumps(stored_data), self.draft_title, self.draft_log, self.draft_password, datetime.now(TZ_SHANGHAI).isoformat())
            )
            await db.commit()
        
        await final_msg.edit(view=DownloadView(self.bot))
        await dm.send(content=f"保护贴已发布！\n跳转链接：{final_msg.jump_url}")
        await interaction.followup.send("✅ 发布成功！", ephemeral=True)

# --- Unlock Modal ---

class PasswordUnlockModal(ui.Modal, title="请输入口令"):
    password_input = ui.TextInput(label="口令", placeholder="请输入...", max_length=50)
    def __init__(self, correct_password, item_row, bot, unlock_type): 
        super().__init__()
        self.c = correct_password
        self.row = item_row 
        self.bot = bot
        self.ut = unlock_type
    
    async def on_submit(self, i: discord.Interaction):
        if i.data['components'][0]['components'][0]['value'].strip() != self.c: return await i.response.send_message("❌ 口令错误！", ephemeral=True)
        await i.response.defer(ephemeral=True, thinking=True)
        success, msg = await check_requirements_common(i, self.ut, self.row['owner_id'], self.row['message_id'])
        if not success: return await i.followup.send(msg, ephemeral=True)

        try: file_data = json.loads(self.row['storage_urls'])
        except: return await i.followup.send("❌ 数据损坏", ephemeral=True)

        file_results = await fetch_files_common(self.bot, file_data)
        if file_results: 
            await record_download_common(i.user, self.row)
            await i.followup.send(content="🔓 口令正确！文件给你：", files=make_discord_files_common(file_results), ephemeral=True)
        else: 
            await i.followup.send("❌ 文件下载失败，请联系作者。", ephemeral=True)

# --- Published Management ---

class EditPublishedFileModal(ui.Modal, title="修改已发布文件名"):
    name_input = ui.TextInput(label="新文件名 (无需输入后缀)", placeholder="请输入新名字", max_length=100)
    def __init__(self, message_id, file_index, file_data):
        super().__init__()
        self.message_id = message_id
        self.file_index = file_index
        self.file_data = file_data 
        current_name = file_data[file_index].get('filename', 'unknown.ext')
        self.name_stem, self.ext = os.path.splitext(current_name)
        self.name_input.default = self.name_stem

    async def on_submit(self, interaction: discord.Interaction):
        new_stem = self.name_input.value.strip()
        if not new_stem: return await interaction.response.send_message("文件名不能为空！", ephemeral=True)
        new_full_name = f"{new_stem}{self.ext}"
        self.file_data[self.file_index]['filename'] = new_full_name
        async with get_db() as db:
            await db.execute("UPDATE protected_items SET storage_urls = ? WHERE message_id = ?", (json.dumps(self.file_data), self.message_id))
            await db.commit()
        await interaction.response.send_message(f"✅ 修改成功！文件已更名为 `{new_full_name}`", ephemeral=True)

class ManageFilesSelectView(ui.View):
    def __init__(self, message_id, file_data):
        super().__init__(timeout=60)
        self.message_id = message_id
        self.file_data = file_data
        options = []
        for i, f in enumerate(file_data):
            fname = f.get('filename', 'unknown')
            options.append(discord.SelectOption(label=f"{i+1}. {fname[:90]}", value=str(i)))
        self.select = ui.Select(placeholder="选择要重命名的文件...", options=options)
        self.select.callback = self.on_select
        self.add_item(self.select)
    async def on_select(self, interaction: discord.Interaction):
        idx = int(self.select.values[0])
        await interaction.response.send_modal(EditPublishedFileModal(self.message_id, idx, self.file_data))

class PostManagementView(ui.View):
    def __init__(self, message_id, file_data):
        super().__init__(timeout=60)
        self.message_id = message_id
        self.file_data = file_data
    @ui.button(label="✏️ 修改文件名", style=discord.ButtonStyle.primary)
    async def rename_files(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message("请选择要修改的文件：", view=ManageFilesSelectView(self.message_id, self.file_data), ephemeral=True)
    @ui.button(label="🗑️ 删除帖子", style=discord.ButtonStyle.danger)
    async def delete_post(self, interaction: discord.Interaction, button: ui.Button):
        async with get_db() as db: 
            await db.execute("DELETE FROM protected_items WHERE message_id = ?", (self.message_id,))
            await db.commit()
        try: await (await interaction.channel.fetch_message(self.message_id)).delete()
        except: pass
        await interaction.response.edit_message(content="✅ 帖子已删除！", embed=None, view=None)

class PostSelectionView(ui.View):
    def __init__(self, posts_rows):
        super().__init__(timeout=60)
        options = []
        for p in posts_rows:
            title = p['title'][:80]
            dl_count = p['download_count']
            options.append(discord.SelectOption(label=title, value=str(p['message_id']), description=f"下载: {dl_count}次 | ID: {p['message_id']}"))
        self.select = ui.Select(placeholder="选择要管理的帖子...", options=options)
        self.select.callback = self.on_select
        self.add_item(self.select)
        self.posts_map = {str(p['message_id']): p for p in posts_rows}
    async def on_select(self, interaction: discord.Interaction):
        mid_str = self.select.values[0]
        row = self.posts_map[mid_str]
        try: file_data = json.loads(row['storage_urls'])
        except: file_data = []
        embed = discord.Embed(title=f"🔧 管理: {row['title']}", description="请选择操作：", color=0xffd700)
        await interaction.response.edit_message(embed=embed, view=PostManagementView(row['message_id'], file_data))

# --- List View (User) ---

class PostListView(ui.View):
    def __init__(self, bot, posts_rows):
        super().__init__(timeout=600)
        self.bot = bot
        self.posts = posts_rows 
        self.selected_row = None
        options = []
        for p in self.posts:
            title = p['title'][:90]
            ts_str = datetime.fromisoformat(p['created_at']).strftime('%m-%d %H:%M')
            options.append(discord.SelectOption(label=title, description=f"发布于: {ts_str}", value=str(p['message_id']), emoji="📄"))
        self.select_menu = ui.Select(placeholder="🔍 请选择要获取的附件...", options=options, row=0)
        self.select_menu.callback = self.on_select
        self.add_item(self.select_menu)

    async def on_select(self, interaction: discord.Interaction):
        selected_id = int(self.select_menu.values[0])
        self.selected_row = next((p for p in self.posts if p['message_id'] == selected_id), None)
        if not self.selected_row: return await interaction.response.send_message("选择出错，请重试。", ephemeral=True)
        self.btn_download.disabled = False
        try:
            file_data = json.loads(self.selected_row['storage_urls'])
            file_list = "\n".join([f"📄 {f.get('filename','???')}" for f in file_data])
        except: file_list = "解析错误"
        mode_map = {"like": "👍 点赞", "like_comment": "👍💬 点赞+评论", "like_password": "👍🔐 点赞+口令", "like_comment_password": "👍💬🔐 全套验证"}
        embed = discord.Embed(title=f"📂 {self.selected_row['title']}", color=discord.Color.green())
        embed.add_field(name="📋 包含文件", value=file_list[:1000], inline=False)
        embed.add_field(name="🔑 获取条件", value=mode_map.get(self.selected_row['unlock_type'], "未知"), inline=False)
        embed.set_footer(text="请点击下方按钮验证条件并下载")
        await interaction.response.edit_message(embed=embed, view=self)

    @ui.button(label="验证并获取", style=discord.ButtonStyle.success, emoji="🎁", disabled=True, row=1)
    async def btn_download(self, interaction: discord.Interaction, button: ui.Button):
        if not self.selected_row: return
        row = self.selected_row
        unlock_type = row['unlock_type']
        
        if "password" in unlock_type:
            has_test_role = isinstance(interaction.user, discord.Member) and interaction.user.get_role(TEST_ROLE_ID)
            if interaction.user.id == row['owner_id'] and not has_test_role:
                 await interaction.response.defer(ephemeral=True, thinking=True)
                 file_data = json.loads(row['storage_urls'])
                 file_results = await fetch_files_common(self.bot, file_data)
                 if file_results: await interaction.followup.send(content="👑 主人请拿好：", files=make_discord_files_common(file_results), ephemeral=True)
                 return
            await interaction.response.send_modal(PasswordUnlockModal(row['password'], row, self.bot, unlock_type))
        else:
            await interaction.response.defer(ephemeral=True, thinking=True)
            success, msg = await check_requirements_common(interaction, unlock_type, row['owner_id'], row['message_id'])
            if not success: return await interaction.followup.send(msg, ephemeral=True)
            file_data = json.loads(row['storage_urls'])
            file_results = await fetch_files_common(self.bot, file_data)
            today_start_iso = datetime.now(TZ_SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            async with get_db() as db:
                cursor = await db.execute("SELECT COUNT(*) FROM download_log WHERE user_id = ? AND timestamp >= ?", (interaction.user.id, today_start_iso))
                cnt = (await cursor.fetchone())[0]
            if file_results:
                await interaction.followup.send(content=f"🎁 验证通过！\n今日剩余: {DAILY_DOWNLOAD_LIMIT - cnt - 1}/{DAILY_DOWNLOAD_LIMIT}", files=make_discord_files_common(file_results), ephemeral=True)
                await record_download_common(interaction.user, row)
            else:
                await interaction.followup.send("❌ 文件下载失败。", ephemeral=True)

# --- Download View ---
class DownloadView(ui.View):
    def __init__(self, bot, target_message_id=None):
        super().__init__(timeout=None)
        self.bot = bot
        self.target_message_id = target_message_id

    @ui.button(label="获取附件", style=discord.ButtonStyle.primary, emoji="🎁", custom_id="dl_btn_v5")
    async def download_btn(self, interaction: discord.Interaction, button: ui.Button):
        message_id = self.target_message_id if self.target_message_id else interaction.message.id
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute("SELECT * FROM protected_items WHERE message_id = ?", (message_id,))).fetchone()
            if not row: return await interaction.response.send_message("❌ 该附件已被作者删除或失效。", ephemeral=True)
            file_data = json.loads(row['storage_urls'])
            unlock_type = row['unlock_type']
            owner_id = row['owner_id']

        if "password" in unlock_type:
            has_test_role = isinstance(interaction.user, discord.Member) and interaction.user.get_role(TEST_ROLE_ID)
            if interaction.user.id == owner_id and not has_test_role:
                await interaction.response.defer(ephemeral=True, thinking=True)
                file_results = await fetch_files_common(self.bot, file_data)
                if file_results: await interaction.followup.send(content="👑 主人请拿好：", files=make_discord_files_common(file_results), ephemeral=True)
                return
            await interaction.response.send_modal(PasswordUnlockModal(row['password'], row, self.bot, unlock_type))
        else:
            await interaction.response.defer(ephemeral=True, thinking=True)
            success, msg = await check_requirements_common(interaction, unlock_type, owner_id, message_id)
            if not success: return await interaction.followup.send(msg, ephemeral=True)
            file_results = await fetch_files_common(self.bot, file_data)
            today_start_iso = datetime.now(TZ_SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            async with get_db() as db:
                cursor = await db.execute("SELECT COUNT(*) FROM download_log WHERE user_id = ? AND timestamp >= ?", (interaction.user.id, today_start_iso))
                cnt = (await cursor.fetchone())[0]
            if file_results:
                await interaction.followup.send(content=f"🎁 验证通过！\n今日剩余: {DAILY_DOWNLOAD_LIMIT - cnt - 1}/{DAILY_DOWNLOAD_LIMIT}", files=make_discord_files_common(file_results), ephemeral=True)
                await record_download_common(interaction.user, row)
            else:
                await interaction.followup.send("❌ 文件下载失败。", ephemeral=True)

# --- Delete View & Cog ---
class DeleteConfirmView(ui.View):
    def __init__(self, message_id): super().__init__(timeout=60); self.message_id = message_id
    @ui.button(label="确认删除", style=discord.ButtonStyle.danger)
    async def confirm(self, i: discord.Interaction, b: ui.Button):
        async with get_db() as db: await db.execute("DELETE FROM protected_items WHERE message_id = ?", (self.message_id,)); await db.commit()
        try: await (await i.channel.fetch_message(self.message_id)).delete()
        except: pass
        await i.response.edit_message(content="已删除！", view=None, embed=None)
    @ui.button(label="取消", style=discord.ButtonStyle.secondary)
    async def cancel(self, i: discord.Interaction, b: ui.Button): await i.response.edit_message(content="操作取消。", view=None, embed=None)

class ProtectionCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.ctx_menu = app_commands.ContextMenu(name="转为保护附件", callback=self.convert_to_protected)
        self.bot.tree.add_command(self.ctx_menu)
        self.has_started_backfill = False

    maker_group = app_commands.Group(name="贴主", description="[贴主] 附件保护发布与管理工具")
    user_group = app_commands.Group(name="保护附件", description="[用户] 下载与查询附件")
    admin_group = app_commands.Group(name="管理员专用", description="[管理] 系统维护工具")

    async def cog_unload(self):
        self.bot.tree.remove_command(self.ctx_menu.name, type=self.ctx_menu.type)

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.has_started_backfill:
            self.has_started_backfill = True
            self.bot.loop.create_task(self.slow_sync_data())

    async def slow_sync_data(self):
        """低速后台同步旧数据的核心逻辑 (精准定位首楼版)"""
        print("⏳ [后台任务] 开始低速同步旧点赞/评论数据...")
        await self.bot.wait_until_ready()
        
        try:
            async with get_db() as db:
                db.row_factory = aiosqlite.Row
                rows = await (await db.execute("SELECT message_id, channel_id FROM protected_items")).fetchall()
            
            total = len(rows)
            print(f"📦 [后台任务] 发现 {total} 个保护贴需要检查同步。")

            for i, row in enumerate(rows):
                mid = row['message_id'] # Bot 面板消息ID
                cid = row['channel_id'] # 频道/帖子ID
                
                try:
                    channel = self.bot.get_channel(cid) or await self.bot.fetch_channel(cid)
                    if not channel: continue

                    # --- 精准定位首楼 ---
                    target_msg = None
                    if isinstance(channel, discord.Thread):
                        # 如果是帖子，尝试获取首楼 (ID 通常等于 channel.id)
                        try: target_msg = await channel.fetch_message(cid)
                        except discord.NotFound: 
                            try: target_msg = await channel.fetch_message(mid)
                            except: pass
                    else:
                        # 如果是普通频道，直接找 Bot 面板消息
                        try: target_msg = await channel.fetch_message(mid)
                        except: pass
                    
                    if not target_msg:
                        async with get_db() as db:
                            await db.execute("DELETE FROM protected_items WHERE message_id = ?", (mid,))
                            await db.commit()
                        continue

                    # --- 同步点赞 ---
                    for reaction in target_msg.reactions:
                        user_count = 0 
                        async for user in reaction.users(limit=None):
                            user_count += 1
                            if user.bot: continue
                            
                            # 【核心映射】: 读取 target_msg 点赞 -> 写入 message_id (Bot面板ID)
                            async with get_db() as db:
                                await db.execute(
                                    "INSERT OR IGNORE INTO user_likes (user_id, message_id) VALUES (?, ?)", 
                                    (user.id, mid)
                                )
                                await db.commit()
                            
                            if user_count % 50 == 0: await asyncio.sleep(1.5)

                        await asyncio.sleep(1)

                    # --- 同步评论 ---
                    if isinstance(channel, discord.Thread):
                        msg_count = 0
                        async for hist_msg in channel.history(limit=1000): 
                            msg_count += 1
                            if hist_msg.author.bot: continue
                            if is_valid_comment(hist_msg.content):
                                # 同样写入 mid 作为关联ID
                                async with get_db() as db:
                                    await db.execute(
                                        "INSERT OR IGNORE INTO user_comments (user_id, message_id, content) VALUES (?, ?, ?)",
                                        (hist_msg.author.id, mid, hist_msg.content[:50])
                                    )
                                    await db.commit()
                            
                            if msg_count % 50 == 0: await asyncio.sleep(2.0)
                        
                        await asyncio.sleep(3)

                    print(f"🔄 [同步进度] 已处理 {i+1}/{total} 个帖子 (ID: {mid})")
                    await asyncio.sleep(5)

                except Exception as e:
                    print(f"❌ [同步错误] 帖子 {mid}: {e}")
                    await asyncio.sleep(5) 
            
            print("✅ [后台任务] 所有旧数据同步完成！")
        except Exception as e:
            print(f"❌ [后台任务] 致命错误: {e}")

    async def _get_active_posts(self, channel, owner_id=None):
        sql = "SELECT * FROM protected_items WHERE channel_id = ?"
        params = (channel.id,)
        if owner_id: sql += " AND owner_id = ?"; params += (owner_id,)
        sql += " ORDER BY created_at DESC"
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(sql, params)).fetchall()
        active_posts, ids_to_clean = [], []
        for row in rows:
            try: await channel.fetch_message(row['message_id']); active_posts.append(row)
            except discord.NotFound: ids_to_clean.append(row['message_id'])
        if ids_to_clean:
            async with get_db() as db: await db.executemany("DELETE FROM protected_items WHERE message_id = ?", [(i,) for i in ids_to_clean]); await db.commit()
        return active_posts
    
    # --- 监听点赞 (实时映射) ---
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id: return
        
        # 我们需要知道用户点赞的是不是帖子的首楼
        # 从而把这个赞正确记录到对应的 protected_item (Bot面板) 上
        
        async with get_db() as db:
            # 1. 直接检查：用户是不是点赞了 Bot 面板消息 (普通频道情况)
            # 尝试写入 (message_id = payload.message_id)
            await db.execute(
                "INSERT OR IGNORE INTO user_likes (user_id, message_id) VALUES (?, ?)", 
                (payload.user_id, payload.message_id)
            )
            
            # 2. 智能映射：如果用户点赞了首楼 (message_id == channel_id)，我们要找到它对应的 Bot 面板
            if payload.message_id == payload.channel_id:
                # 这是一个帖子首楼的点赞
                # 查找该帖子下所有的保护记录 (通常只有一个)
                db.row_factory = aiosqlite.Row
                cursor = await db.execute("SELECT message_id FROM protected_items WHERE channel_id = ?", (payload.channel_id,))
                rows = await cursor.fetchall()
                for row in rows:
                    # 把这个赞“映射”给该帖子下的保护面板消息
                    await db.execute(
                        "INSERT OR IGNORE INTO user_likes (user_id, message_id) VALUES (?, ?)",
                        (payload.user_id, row['message_id'])
                    )
            
            await db.commit()

    # --- 监听取消点赞 (实时删除映射) ---
    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        async with get_db() as db:
            # 删除直接记录
            await db.execute(
                "DELETE FROM user_likes WHERE user_id = ? AND message_id = ?", 
                (payload.user_id, payload.message_id)
            )
            
            # 删除映射记录 (如果是首楼取消赞)
            if payload.message_id == payload.channel_id:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute("SELECT message_id FROM protected_items WHERE channel_id = ?", (payload.channel_id,))
                rows = await cursor.fetchall()
                for row in rows:
                    await db.execute(
                        "DELETE FROM user_likes WHERE user_id = ? AND message_id = ?",
                        (payload.user_id, row['message_id'])
                    )
            
            await db.commit()

    # --- 监听评论 (实时存库) ---
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot: return
        if not isinstance(message.channel, discord.Thread): return

        if is_valid_comment(message.content):
            # 获取帖子ID (channel.id)
            thread_id = message.channel.id 
            
            # 我们需要把评论关联到该帖子下的所有保护记录 (Bot面板ID)
            async with get_db() as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute("SELECT message_id FROM protected_items WHERE channel_id = ?", (thread_id,))
                rows = await cursor.fetchall()
                
                for row in rows:
                    panel_msg_id = row['message_id']
                    await db.execute(
                        "INSERT OR REPLACE INTO user_comments (user_id, message_id, content) VALUES (?, ?, ?)", 
                        (message.author.id, panel_msg_id, message.content[:50]) 
                    )
                await db.commit()

    # ... (管理命令组，保持不变) ...

    @admin_group.command(name="修复面板", description="刷新本频道所有旧面板")
    async def fix_panels(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute("SELECT * FROM protected_items WHERE channel_id = ?", (interaction.channel.id,))).fetchall()
        if not rows: return await interaction.followup.send("本频道在数据库中没有活跃记录。", ephemeral=True)
        success_count, fail_count = 0, 0
        for row in rows:
            try:
                msg = await interaction.channel.fetch_message(row['message_id'])
                new_view = DownloadView(self.bot)
                await msg.edit(view=new_view)
                success_count += 1
                await asyncio.sleep(1.0) 
            except: fail_count += 1
        await interaction.followup.send(f"✅ 修复完成！\n成功刷新: {success_count} 个\n失败/已删除: {fail_count} 个", ephemeral=True)

    @user_group.command(name="今日下载记录", description="查询今日下载历史和剩余次数")
    async def my_downloads_today(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        today_start_iso = datetime.now(TZ_SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT title, filenames, timestamp FROM download_log WHERE user_id = ? AND timestamp >= ? ORDER BY timestamp DESC", (interaction.user.id, today_start_iso))
            logs = await cursor.fetchall()
        download_count = len(logs)
        remaining = DAILY_DOWNLOAD_LIMIT - download_count
        embed = discord.Embed(title=f"📜 {interaction.user.display_name} 的今日下载记录", color=discord.Color.blue())
        embed.description = f"**今日下载次数**: {download_count}/{DAILY_DOWNLOAD_LIMIT}\n**剩余次数**: {remaining}"
        if not logs: embed.add_field(name="记录", value="今天还没有下载过任何附件哦。")
        else:
            log_text = ""
            for log in logs:
                try: filenames = ", ".join(json.loads(log['filenames']))
                except: filenames = "未知文件"
                ts = discord.utils.format_dt(datetime.fromisoformat(log['timestamp']), 'T')
                log_text += f"- **{log['title']}**: `{filenames}` ({ts})\n"
            embed.add_field(name="详细记录", value=log_text[:1024], inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @user_group.command(name="获取附件", description="显示本频道最近的5个受保护附件列表")
    async def get_attachments_list(self, interaction: discord.Interaction):
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM protected_items WHERE channel_id = ? ORDER BY created_at DESC LIMIT 5", (interaction.channel.id,))
            rows = await cursor.fetchall()
        if not rows: return await interaction.response.send_message("❌ 本频道没有任何受保护的附件记录。", ephemeral=True)
        view = PostListView(self.bot, rows)
        embed = discord.Embed(title="📂 附件获取列表", description=f"发现本频道有 **{len(rows)}** 个最近的附件包。\n请在下方下拉菜单中选择一个进行查看和下载。", color=0x87ceeb)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @maker_group.command(name="管理附件", description="查看和管理我发布的保护贴及附件")
    async def manage_attachments(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        posts = await self._get_active_posts(interaction.channel, owner_id=interaction.user.id)
        if not posts: return await interaction.followup.send("你在这个频道还没有发过活跃的保护贴。", ephemeral=True)
        
        embed = discord.Embed(title=f"👑 {interaction.user.display_name} 的管理面板", color=0xffd700, description="请在下方选择一个帖子进行管理（重命名附件或删除）。")
        view = PostSelectionView(posts)
        
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    async def convert_to_protected(self, interaction: discord.Interaction, message: discord.Message):
        if message.author != interaction.user: return await interaction.response.send_message("不可以动别人的东西！", ephemeral=True)
        if not message.attachments: return await interaction.response.send_message("消息里没有附件？", ephemeral=True)
        view = ProtectionDraftView(self.bot, interaction.user, message.attachments, target_message=message, default_log=message.content or None)
        embed = discord.Embed(title="🚀 正在启动保护向导...", color=0x87ceeb)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        await view.update_dashboard(interaction)

    @maker_group.command(name="设置附件保护", description="上传文件并创建保护贴")
    @app_commands.describe(file1="附件1", file2="附件2", file3="附件3", file4="附件4", file5="附件5", file6="附件6", file7="附件7", file8="附件8", file9="附件9", file10="附件10")
    async def create_protection(self, interaction: discord.Interaction, file1: discord.Attachment, file2: discord.Attachment=None, file3: discord.Attachment=None, file4: discord.Attachment=None, file5: discord.Attachment=None, file6: discord.Attachment=None, file7: discord.Attachment=None, file8: discord.Attachment=None, file9: discord.Attachment=None, file10: discord.Attachment=None):
        attachments = [f for f in [file1, file2, file3, file4, file5, file6, file7, file8, file9, file10] if f]
        if not attachments: return await interaction.response.send_message("请至少上传一个文件！", ephemeral=True)
        view = ProtectionDraftView(self.bot, interaction.user, attachments)
        embed = discord.Embed(title="🚀 正在启动保护向导...", color=0x87ceeb)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        await view.update_dashboard(interaction)

async def setup(bot):
    await bot.add_cog(ProtectionCog(bot))
