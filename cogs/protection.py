import discord
from discord import app_commands, ui
from discord.ext import commands
import json
import asyncio
import io
import re
from datetime import datetime
from zoneinfo import ZoneInfo
import aiosqlite

from database import get_db

TZ_SHANGHAI = ZoneInfo("Asia/Shanghai")
DAILY_DOWNLOAD_LIMIT = 50
TEST_ROLE_ID = 1402290127627091979

# --- Global Helpers ---

def is_valid_comment(content: str) -> bool:
    if not content: return False
    content = re.sub(r'<a?:.+?:\d+>', '', content)
    content = re.sub(r'http\S+', '', content)
    return len(content.strip()) > 5

def get_requirement_text(unlock_type: str) -> str:
    mapping = {
        "like": "👍 需要 [点赞首楼]",
        "like_comment": "👍💬 需要 [点赞首楼 + 帖子内有效评论]",
        "like_password": "👍🔐 需要 [点赞首楼 + 输入口令]",
        "like_comment_password": "👍💬🔐 需要 [点赞首楼 + 有效评论 + 输入口令]"
    }
    return mapping.get(unlock_type, "未知条件")

# --- Modal Classes ---

class DraftTitleModal(ui.Modal, title="设置标题"):
    title_input = ui.TextInput(label="标题", placeholder="请输入...", max_length=100)
    def __init__(self, view): super().__init__(); self.view_ref = view; self.title_input.default = view.draft_title
    async def on_submit(self, i: discord.Interaction): self.view_ref.draft_title = self.title_input.value; await self.view_ref.update_dashboard(i)

class DraftNoteModal(ui.Modal, title="设置作者提示"):
    log_input = ui.TextInput(label="说明/日志", style=discord.TextStyle.paragraph, placeholder="写点什么...", max_length=4000, required=False)
    def __init__(self, view): super().__init__(); self.view_ref = view; self.log_input.default = view.draft_log[:4000] if view.draft_log else None
    async def on_submit(self, i: discord.Interaction): self.view_ref.draft_log = self.log_input.value; await self.view_ref.update_dashboard(i)

class DraftPasswordModal(ui.Modal, title="设置口令"):
    pwd_input = ui.TextInput(label="下载口令", placeholder="1-100字", min_length=1, max_length=100)
    def __init__(self, view, next_mode): super().__init__(); self.view_ref = view; self.next_mode = next_mode; self.pwd_input.default = view.draft_password
    async def on_submit(self, i: discord.Interaction):
        clean_pwd = self.pwd_input.value.strip()
        if not clean_pwd: return await i.response.send_message("口令不能为空！", ephemeral=True)
        self.view_ref.draft_password = clean_pwd; self.view_ref.draft_mode = self.next_mode; await self.view_ref.update_dashboard(i)

# --- Core Logic Handler (The "Engine") ---

class ProtectionLogic:
    """封装所有下载验证和文件获取的逻辑，供不同 View 调用"""
    def __init__(self, bot):
        self.bot = bot

    async def check_requirements(self, interaction, unlock_type, owner_id):
        # 1. 特权检测
        has_test_role = False
        if isinstance(interaction.user, discord.Member) and interaction.user.get_role(TEST_ROLE_ID):
            has_test_role = True
        is_owner = (interaction.user.id == owner_id)
        if is_owner and not has_test_role: return True, "owner"

        # 2. 每日限制
        today = datetime.now(TZ_SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT COUNT(*) FROM download_log WHERE user_id = ? AND timestamp >= ?", (interaction.user.id, today))
            count = (await cursor.fetchone())[0]
        if count >= DAILY_DOWNLOAD_LIMIT:
            return False, f"⚠️ 今日下载次数已达上限 ({DAILY_DOWNLOAD_LIMIT}/{DAILY_DOWNLOAD_LIMIT})"

        # 3. 点赞检测 (首楼)
        target_msg = None
        async for msg in interaction.channel.history(limit=1, oldest_first=True): target_msg = msg; break
        if not target_msg: return False, "❌ 无法定位帖子首楼"

        reacted = False
        for r in target_msg.reactions:
            async for u in r.users():
                if u.id == interaction.user.id: reacted = True; break
            if reacted: break
        if not reacted:
            return False, f"🛑 请先对 **[帖子首楼]({target_msg.jump_url})** 点赞再获取附件。"

        # 4. 评论检测
        if "comment" in unlock_type:
            has_commented = False
            # 在当前频道搜索用户评论 (限制搜索范围防止超时)
            async for msg in interaction.channel.history(limit=150):
                if msg.author.id == interaction.user.id and is_valid_comment(msg.content):
                    has_commented = True; break
            if not has_commented:
                return False, "💬 未检测到您的有效评论（需5字以上非表情内容）。"
        
        return True, "passed"

    async def fetch_files(self, file_data):
        results = []
        for item in file_data:
            url = item.get('url')
            if item.get('strategy') == 'msg_ref':
                try:
                    ch = self.bot.get_channel(item['channel_id']) or await self.bot.fetch_channel(item['channel_id'])
                    msg = await ch.fetch_message(item['message_id'])
                    url = msg.attachments[item['attachment_index']].url
                except: pass
            if not url: continue
            try:
                async with self.bot.http_session.get(url) as resp:
                    if resp.status == 200:
                        results.append({'filename': item.get('filename', 'file'), 'bytes': await resp.read()})
            except: pass
        return results

    def make_discord_files(self, results):
        return [discord.File(io.BytesIO(r['bytes']), filename=r['filename']) for r in results]

    async def record_download(self, user, row):
        async with get_db() as db:
            await db.execute("UPDATE protected_items SET download_count = download_count + 1 WHERE message_id = ?", (row['message_id'],))
            file_data = json.loads(row['storage_urls'])
            names = json.dumps([f.get('filename','unknown') for f in file_data])
            await db.execute("INSERT INTO download_log (user_id, message_id, title, filenames, timestamp) VALUES (?, ?, ?, ?, ?)", 
                           (user.id, row['message_id'], row['title'], names, datetime.now(TZ_SHANGHAI).isoformat()))
            await db.commit()

# --- Unlock Modal ---

class PasswordUnlockModal(ui.Modal, title="请输入口令"):
    password_input = ui.TextInput(label="口令", placeholder="请输入下载口令...", max_length=100)
    def __init__(self, correct_pwd, row, engine): 
        super().__init__()
        self.correct_pwd = correct_pwd
        self.row = row
        self.engine = engine

    async def on_submit(self, i: discord.Interaction):
        if self.password_input.value.strip() != self.correct_pwd:
            return await i.response.send_message("❌ 口令错误！", ephemeral=True)
        
        await i.response.defer(ephemeral=True, thinking=True)
        success, msg = await self.engine.check_requirements(i, self.row['unlock_type'], self.row['owner_id'])
        if not success: return await i.followup.send(msg, ephemeral=True)

        file_data = json.loads(self.row['storage_urls'])
        results = await self.engine.fetch_files(file_data)
        if results:
            await self.engine.record_download(i.user, self.row)
            await i.followup.send("🔓 口令验证通过！", files=self.engine.make_discord_files(results), ephemeral=True)
        else:
            await i.followup.send("❌ 文件获取失败（源文件可能已过期）。", ephemeral=True)

# --- Download Views ---

class DownloadView(ui.View):
    """显示在帖子里的公共按钮视图"""
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.engine = ProtectionLogic(bot)

    @ui.button(label="获取附件", style=discord.ButtonStyle.primary, emoji="🎁", custom_id="dl_btn_v4")
    async def download_btn(self, interaction: discord.Interaction, button: ui.Button):
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute("SELECT * FROM protected_items WHERE message_id = ?", (interaction.message.id,))).fetchone()
        
        if not row: return await interaction.response.send_message("❌ 该附件已失效。", ephemeral=True)

        if "password" in row['unlock_type']:
            # 拥有者特权跳过密码
            if interaction.user.id == row['owner_id'] and not interaction.user.get_role(TEST_ROLE_ID):
                await interaction.response.defer(ephemeral=True, thinking=True)
                results = await self.engine.fetch_files(json.loads(row['storage_urls']))
                return await interaction.followup.send("👑 主人请收好：", files=self.engine.make_discord_files(results), ephemeral=True)
            
            await interaction.response.send_modal(PasswordUnlockModal(row['password'], row, self.engine))
        else:
            await interaction.response.defer(ephemeral=True, thinking=True)
            success, msg = await self.engine.check_requirements(interaction, row['unlock_type'], row['owner_id'])
            if not success: return await i.followup.send(msg, ephemeral=True)
            
            results = await self.engine.fetch_files(json.loads(row['storage_urls']))
            if results:
                await self.engine.record_download(interaction.user, row)
                await interaction.followup.send("✅ 验证通过！", files=self.engine.make_discord_files(results), ephemeral=True)
            else:
                await interaction.followup.send("❌ 获取失败。", ephemeral=True)

class EphemeralDownloadView(ui.View):
    """执行 /获取附件 时弹出的私有视图"""
    def __init__(self, bot, items_rows):
        super().__init__(timeout=600)
        self.engine = ProtectionLogic(bot)
        for row in items_rows[:12]: # 限制展示数量防止按钮过多
            btn = ui.Button(label=f"获取: {row['title']}"[:80], style=discord.ButtonStyle.success, emoji="📥")
            btn.callback = self.create_callback(row)
            self.add_item(btn)

    def create_callback(self, row):
        async def callback(interaction: discord.Interaction):
            if "password" in row['unlock_type']:
                if interaction.user.id == row['owner_id'] and not interaction.user.get_role(TEST_ROLE_ID):
                    await interaction.response.defer(ephemeral=True, thinking=True)
                    res = await self.engine.fetch_files(json.loads(row['storage_urls']))
                    return await interaction.followup.send("👑 拿好：", files=self.engine.make_discord_files(res), ephemeral=True)
                await interaction.response.send_modal(PasswordUnlockModal(row['password'], row, self.engine))
            else:
                await interaction.response.defer(ephemeral=True, thinking=True)
                success, msg = await self.engine.check_requirements(interaction, row['unlock_type'], row['owner_id'])
                if not success: return await interaction.followup.send(msg, ephemeral=True)
                res = await self.engine.fetch_files(json.loads(row['storage_urls']))
                if res:
                    await self.engine.record_download(interaction.user, row)
                    await interaction.followup.send("✅ 验证成功！", files=self.engine.make_discord_files(res), ephemeral=True)
                else:
                    await interaction.followup.send("❌ 失败。", ephemeral=True)
        return callback

# --- Creator Draft View ---

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
    
    async def update_dashboard(self, interaction: discord.Interaction):
        log_preview = self.draft_log[:50] + "..." if self.draft_log and len(self.draft_log) > 50 else self.draft_log
        status_desc = (f"📦 **已传文件**: {len(self.attachments)} 个\n🏷️ **当前标题**: {self.draft_title}\n📝 **作者提示**: {'✅ ' + log_preview if self.draft_log else '⚪ 未设置'}\n")
        mode_map = {"like": "👍 点赞解锁", "like_comment": "💬 点赞+评论", "like_password": f"🔐 点赞+口令 (口令: ||{self.draft_password}||)", "like_comment_password": f"🔐💬 点赞+评论+口令 (口令: ||{self.draft_password}||)"}
        status_desc += f"⚙️ **获取方式**: {mode_map.get(self.draft_mode)}"
        guide_desc = ("1️⃣ 点击 **第一排** 按钮修改标题或添加说明。\n2️⃣ 点击 **第二排** 按钮选择解锁条件。\n3️⃣ 确认无误后，点击底部的 **🚀 确认发布**。")
        embed = discord.Embed(title="🛠️ 附件保护控制台", color=0x87ceeb); embed.add_field(name="📊 当前配置状态", value=status_desc, inline=False); embed.add_field(name="📖 操作指引", value=guide_desc, inline=False); embed.set_footer(text="此面板仅你自己可见")
        
        if interaction.response.is_done(): await interaction.edit_original_response(content=None, embed=embed, view=self)
        else: await interaction.response.edit_message(content=None, embed=embed, view=self)

    @ui.button(label="修改标题", style=discord.ButtonStyle.secondary, row=0, emoji="🏷️")
    async def btn_set_title(self, i: discord.Interaction, b: ui.Button): await i.response.send_modal(DraftTitleModal(self))
    @ui.button(label="作者提示", style=discord.ButtonStyle.secondary, row=0, emoji="📝")
    async def btn_set_note(self, i: discord.Interaction, b: ui.Button): await i.response.send_modal(DraftNoteModal(self))
    
    @ui.button(label="点赞", style=discord.ButtonStyle.primary, row=1)
    async def mode_like(self, i, b): self.draft_mode = "like"; await self.update_dashboard(i)
    @ui.button(label="点赞+评论", style=discord.ButtonStyle.primary, row=1)
    async def mode_like_comment(self, i, b): self.draft_mode = "like_comment"; await self.update_dashboard(i)
    @ui.button(label="点赞+口令", style=discord.ButtonStyle.success, row=1, emoji="🔐")
    async def mode_like_pass(self, i, b): await i.response.send_modal(DraftPasswordModal(self, "like_password"))
    @ui.button(label="点赞+评论+口令", style=discord.ButtonStyle.success, row=1, emoji="🔐")
    async def mode_like_comm_pass(self, i, b): await i.response.send_modal(DraftPasswordModal(self, "like_comment_password"))
    
    @ui.button(label="确认发布", style=discord.ButtonStyle.danger, row=2, emoji="🚀")
    async def btn_confirm(self, i, b): 
        await i.response.edit_message(content="⏳ 正在加密上传...", embed=None, view=None)
        await self.publish(i)

    @ui.button(label="取消", style=discord.ButtonStyle.gray, row=2, emoji="✖️")
    async def btn_cancel(self, i, b): await i.response.edit_message(content="操作已取消。", embed=None, view=None); self.stop()

    async def publish(self, interaction: discord.Interaction):
        # 1. 备份文件到私信
        files_to_send, stored_data = [], []
        try:
            for att in self.attachments:
                file_bytes = await att.read()
                files_to_send.append(discord.File(io.BytesIO(file_bytes), filename=att.filename))
            dm = await self.user.create_dm()
            backup_msg = await dm.send(content=f"【{self.draft_title}】备份", files=files_to_send)
            for idx, att in enumerate(backup_msg.attachments):
                stored_data.append({"strategy": "msg_ref", "channel_id": backup_msg.channel.id, "message_id": backup_msg.id, "attachment_index": idx, "filename": att.filename})
        except: return await interaction.followup.send("发布失败，请确保已开启私信！", ephemeral=True)

        # 2. 生成主贴
        if self.target_message: 
            try: await self.target_message.delete()
            except: pass

        embed = discord.Embed(title=f"✨ {self.draft_title}", description=self.draft_log or "附件已加密保护", color=0xffb7c5)
        embed.set_author(name=f"由 {self.user.display_name} 发布", icon_url=self.user.display_avatar.url)
        embed.add_field(name="🔑 获取条件", value=get_requirement_text(self.draft_mode), inline=True)
        embed.add_field(name="📦 文件数量", value=f"**{len(stored_data)}** 个", inline=True)
        embed.set_footer(text="由 创作保护助手 强力驱动", icon_url=self.bot.user.display_avatar.url)
        
        final_msg = await interaction.channel.send(embed=embed)
        try: await final_msg.pin()
        except: pass
        
        async with get_db() as db:
            await db.execute(
                "INSERT INTO protected_items (message_id, channel_id, owner_id, unlock_type, storage_urls, title, log, password, created_at) VALUES (?,?,?,?,?,?,?,?,?)", 
                (final_msg.id, final_msg.channel.id, self.user.id, self.draft_mode, json.dumps(stored_data), self.draft_title, self.draft_log, self.draft_password, datetime.now(TZ_SHANGHAI).isoformat())
            )
            await db.commit()
        
        await final_msg.edit(view=DownloadView(self.bot))
        await interaction.followup.send("✅ 发布成功！", ephemeral=True)

# --- Delete Confirmation ---

class DeleteConfirmView(ui.View):
    def __init__(self, message_id): super().__init__(timeout=60); self.message_id = message_id
    @ui.button(label="确认删除", style=discord.ButtonStyle.danger)
    async def confirm(self, i, b):
        async with get_db() as db: await db.execute("DELETE FROM protected_items WHERE message_id = ?", (self.message_id,)); await db.commit()
        try: await (await i.channel.fetch_message(self.message_id)).delete()
        except: pass
        await i.response.edit_message(content="已删除！", view=None, embed=None)

# --- Cog Implementation ---

class ProtectionCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.ctx_menu = app_commands.ContextMenu(name="转为保护附件", callback=self.convert_to_protected)
        self.bot.tree.add_command(self.ctx_menu)
    
    async def _get_active_posts(self, channel):
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute("SELECT * FROM protected_items WHERE channel_id = ? ORDER BY created_at DESC", (channel.id,))).fetchall()
        active = []
        for row in rows:
            try: await channel.fetch_message(row['message_id']); active.append(row)
            except: pass
        return active

    @app_commands.command(name="获取附件", description="私密查看本贴内所有附件及下载入口")
    async def get_attachments(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        posts = await self._get_active(i.channel)
        if not posts: return await i.followup.send("🔍 没找到活跃的保护附件。", ephemeral=True)

        embed = discord.Embed(title=f"📦 本贴共有 {len(posts)} 组附件", color=0x87ceeb)
        for p in posts[:10]:
            try:
                files = json.loads(p['storage_urls'])
                file_str = "\n".join([f"- `📄 {f['filename']}`" for f in files])
            except: file_str = "解析失败"
            
            jump_url = f"https://discord.com/channels/{i.guild_id}/{i.channel.id}/{p['message_id']}"
            cond = get_requirement_text(p['unlock_type'])
            
            embed.add_field(
                name=f"📌 {p['title']}", 
                value=f"**文件:**\n{file_str}\n**条件:** {cond}\n[🔗 点击跳转到该位置]({jump_url})", 
                inline=False
            )
        await i.followup.send(embed=embed, view=EphemeralDownloadView(self.bot, posts), ephemeral=True)

    @app_commands.command(name="管理附件", description="管理我发布的保护贴")
    async def manage_attachments(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            posts = await (await db.execute("SELECT * FROM protected_items WHERE owner_id = ? AND channel_id = ?", (interaction.user.id, interaction.channel.id))).fetchall()
        if not posts: return await interaction.followup.send("你在这里没有发布过内容。", ephemeral=True)
        
        options = [discord.SelectOption(label=p['title'][:50], value=str(p['message_id'])) for p in posts[:25]]
        select = ui.Select(placeholder="选择要删除的帖子...", options=options)
        async def callback(inter): await inter.response.send_message("确定删除吗？", view=DeleteConfirmView(int(select.values[0])), ephemeral=True)
        select.callback = callback
        await interaction.followup.send("选择一个帖子进行管理：", view=ui.View().add_item(select), ephemeral=True)

    @app_commands.command(name="设置附件保护", description="上传文件并创建保护贴")
    async def create_protection(self, interaction: discord.Interaction, file1: discord.Attachment, file2: discord.Attachment=None, file3: discord.Attachment=None):
        files = [f for f in [file1, file2, file3] if f]
        view = ProtectionDraftView(self.bot, interaction.user, files)
        await interaction.response.send_message("🚀 启动保护向导...", view=view, ephemeral=True)
        await view.update_dashboard(interaction)

    async def convert_to_protected(self, interaction: discord.Interaction, message: discord.Message):
        if message.author != interaction.user: return await interaction.response.send_message("只能转换自己的消息！", ephemeral=True)
        if not message.attachments: return await interaction.response.send_message("该消息没有附件！", ephemeral=True)
        view = ProtectionDraftView(self.bot, interaction.user, message.attachments, target_message=message, default_log=message.content)
        await interaction.response.send_message("🚀 启动转换向导...", view=view, ephemeral=True)
        await view.update_dashboard(interaction)

async def setup(bot):
    await bot.add_cog(ProtectionCog(bot))
