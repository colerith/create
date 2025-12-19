import discord
from discord import app_commands, ui
from discord.ext import commands
import json
import asyncio
import io
import re
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
    content = re.sub(r'<a?:.+?:\d+>', '', content)
    content = re.sub(r'http\S+', '', content)
    return len(content.strip()) > 5

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
    @ui.button(label="查看已传文件", style=discord.ButtonStyle.secondary, row=0, emoji="📦")
    async def btn_view_files(self, i: discord.Interaction, b: ui.Button): 
        names = "\n".join([f"- {f.filename}" for f in self.attachments])
        await i.response.send_message(f"已准备文件：\n{names[:1900]}", ephemeral=True)
    
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
            for att in self.attachments: 
                file_bytes = await att.read()
                f = discord.File(io.BytesIO(file_bytes), filename=att.filename)
                files_to_send.append(f)
        except Exception as e: return await interaction.followup.send(f"文件读取失败：{e}", ephemeral=True)
        
        stored_data = []
        try:
            dm = await self.user.create_dm()
            backup_msg = await dm.send(content=f"【{self.draft_title}】的备份！\nID: {interaction.id}", files=files_to_send)
            
            for i, att in enumerate(backup_msg.attachments):
                stored_data.append({
                    "strategy": "msg_ref",
                    "channel_id": backup_msg.channel.id,
                    "message_id": backup_msg.id,
                    "attachment_index": i,
                    "filename": att.filename,
                    "url": att.url
                })
        except discord.Forbidden: 
            return await interaction.followup.send("无法私信备份（请开启私信权限）！", ephemeral=True)
        except Exception as e:
            return await interaction.followup.send(f"备份失败：{e}", ephemeral=True)

        if self.target_message:
            try: await self.target_message.delete()
            except: pass
            
        final_desc = self.draft_log if self.draft_log else "一份受保护的附件已发布，满足条件即可获取。"
        embed = discord.Embed(title=f"✨ {self.draft_title}", description=final_desc, color=discord.Color.from_rgb(255, 183, 197))
        embed.set_author(name=f"由 {self.user.display_name} 发布", icon_url=self.user.display_avatar.url)
        mode_map = {
            "like": "👍 **点赞首楼**", 
            "like_comment": "👍💬 **点赞首楼 + 回复本贴**", 
            "like_password": "👍🔐 **点赞首楼 + 口令**", 
            "like_comment_password": "👍💬🔐 **点赞首楼 + 回复本贴 + 口令**"
        }
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
    
    # 接收 view_ref 以调用 check_requirements
    def __init__(self, correct_password, item_row, view_ref, unlock_type): 
        super().__init__()
        self.c = correct_password
        self.row = item_row 
        self.v = view_ref
        self.ut = unlock_type
    
    async def on_submit(self, i: discord.Interaction):
        # 1. 验证口令
        if i.data['components'][0]['components'][0]['value'].strip() != self.c: 
            return await i.response.send_message("❌ 口令错误！", ephemeral=True)
        
        # 2. 立即 Defer（防止后续耗时操作超时）
        await i.response.defer(ephemeral=True, thinking=True)
        
        # 3. 执行点赞/评论检测 (耗时操作放在 Defer 之后)
        success, msg = await self.v.check_requirements(i, self.ut, self.row['owner_id'])
        if not success:
            return await i.followup.send(msg, ephemeral=True)

        try:
            file_data = json.loads(self.row['storage_urls'])
        except Exception as e:
            return await i.followup.send(f"❌ 数据损坏: {e}", ephemeral=True)

        # 4. 下载并发送
        file_results = await self.v.fetch_files(file_data)
        if file_results: 
            self.v.record_download(i.user, self.row)
            await i.followup.send(content="🔓 口令正确！文件给你：", files=self.v.make_discord_files(file_results), ephemeral=True)
            await self.v.send_dm_backup(i.user, file_results)
        else: 
            await i.followup.send("❌ 文件下载失败（可能源文件已过期），请联系作者。", ephemeral=True)

# --- Download View ---

class DownloadView(ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot
    
    async def fetch_files(self, file_data):
        results = []
        if not isinstance(file_data, list): return []

        for item in file_data:
            if not isinstance(item, dict): continue
            download_url = item.get('url')
            
            if item.get('strategy') == 'msg_ref':
                try:
                    try:
                        channel = self.bot.get_channel(item['channel_id']) or await self.bot.fetch_channel(item['channel_id'])
                        msg = await channel.fetch_message(item['message_id'])
                        idx = item.get('attachment_index', 0)
                        if 0 <= idx < len(msg.attachments):
                            download_url = msg.attachments[idx].url
                    except: pass
                except: pass

            if not download_url: continue

            try:
                async with self.bot.http_session.get(download_url) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        if len(data) > 0:
                            results.append({
                                'filename': item.get('filename', 'unknown'),
                                'bytes': data
                            })
            except Exception as e: print(f"DL Error: {e}")
        return results

    def make_discord_files(self, file_results):
        files = []
        for res in file_results:
            files.append(discord.File(io.BytesIO(res['bytes']), filename=res['filename']))
        return files

    async def send_dm_backup(self, user: discord.Member, file_results):
        files = self.make_discord_files(file_results)
        if not files: return
        try: await user.send(content="这是您刚刚下载的附件备份：", files=files)
        except: pass

    # 【新增】：将耗时的检测逻辑剥离
    async def check_requirements(self, interaction, unlock_type, owner_id):
        # 1. 身份特权检测
        has_test_role = False
        if isinstance(interaction.user, discord.Member) and interaction.user.get_role(TEST_ROLE_ID):
            has_test_role = True
        
        is_owner = (interaction.user.id == owner_id)
        if is_owner and has_test_role: is_owner = False 

        if is_owner: return True, "owner" # 特权直接通过

        # 2. 每日下载限制
        today_start_iso = datetime.now(TZ_SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT COUNT(*) FROM download_log WHERE user_id = ? AND timestamp >= ?", (interaction.user.id, today_start_iso))
            download_count = (await cursor.fetchone())[0]
        if download_count >= DAILY_DOWNLOAD_LIMIT:
            return False, f"⚠️ 您今日的下载次数已达上限（{DAILY_DOWNLOAD_LIMIT}/{DAILY_DOWNLOAD_LIMIT}）。"

        # 3. 点赞检测
        target_msg = None
        try:
            async for msg in interaction.channel.history(limit=1, oldest_first=True):
                target_msg = msg; break
        except: pass
        
        if not target_msg: return False, "❌ 无法定位首楼，请联系管理员。"

        reacted = False
        for r in target_msg.reactions:
            async for u in r.users():
                if u.id == interaction.user.id: reacted = True; break
            if reacted: break
        
        if not reacted:
            return False, f"🛑 请先对 **[帖子首楼]({target_msg.jump_url})** 点赞才能继续下载唷！"

        # 4. 评论检测
        if "comment" in unlock_type:
            has_commented = False
            try:
                # 扫描当前面板消息之后的消息
                async for msg in interaction.channel.history(after=interaction.message, limit=100):
                    if msg.author.id == interaction.user.id:
                        if is_valid_comment(msg.content):
                            has_commented = True; break
            except: pass
            
            if not has_commented:
                return False, "💬 检测不到您的有效评论捏！\n请先在 **当前帖子底部** 发送一条评论（字数>5），然后再点击按钮。"

        return True, "passed"

    @ui.button(label="获取附件", style=discord.ButtonStyle.primary, emoji="🎁", custom_id="dl_btn_v4")
    async def download_btn(self, interaction: discord.Interaction, button: ui.Button):
        message_id = interaction.message.id
        
        # 1. 快速读取数据库 (通常很快)
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute("SELECT * FROM protected_items WHERE message_id = ?", (message_id,))).fetchone()
            
            if not row:
                button.disabled = True
                await interaction.message.edit(view=self)
                return await interaction.response.send_message("❌ 该附件已被作者删除或失效。", ephemeral=True)
            
            try:
                file_data = json.loads(row['storage_urls'])
            except:
                return await interaction.response.send_message("❌ 数据库记录损坏。", ephemeral=True)
            
            unlock_type = row['unlock_type']
            owner_id = row['owner_id']

        # --- 2. 核心分支逻辑 ---
        # 如果需要密码，必须立刻弹出 Modal (不能先 Defer)
        # 如果不需要密码，必须立刻 Defer (防止耗时检测导致超时)

        if "password" in unlock_type:
            # 拥有者特权检查 (简单版，不查 DB 频率，因为拥有者无需频率限制)
            # 如果是拥有者且不是测试员，直接跳过密码
            has_test_role = False
            if isinstance(interaction.user, discord.Member) and interaction.user.get_role(TEST_ROLE_ID):
                has_test_role = True
            
            # 真正的拥有者直接下载，不弹窗
            if interaction.user.id == owner_id and not has_test_role:
                await interaction.response.defer(ephemeral=True, thinking=True)
                file_results = await self.fetch_files(file_data)
                if file_results:
                    await interaction.followup.send(content="👑 主人请拿好：", files=self.make_discord_files(file_results), ephemeral=True)
                return

            # 普通用户 -> 弹窗 (把检测任务交给 Modal 的 on_submit)
            await interaction.response.send_modal(PasswordUnlockModal(row['password'], row, self, unlock_type))
        
        else:
            # 无需密码 -> 立即 Defer
            await interaction.response.defer(ephemeral=True, thinking=True)
            
            # 然后再做耗时的 API 检测
            success, msg = await self.check_requirements(interaction, unlock_type, owner_id)
            if not success:
                return await interaction.followup.send(msg, ephemeral=True)

            # 通过 -> 下载发送
            file_results = await self.fetch_files(file_data)
            
            # 计算剩余次数用于展示
            today_start_iso = datetime.now(TZ_SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            async with get_db() as db:
                cursor = await db.execute("SELECT COUNT(*) FROM download_log WHERE user_id = ? AND timestamp >= ?", (interaction.user.id, today_start_iso))
                cnt = (await cursor.fetchone())[0]
            remaining = DAILY_DOWNLOAD_LIMIT - (cnt + 1)
            
            status_msg = f"本次下载后，今日剩余: {remaining}/{DAILY_DOWNLOAD_LIMIT}"
            optional_tip = "\n*(提示：如果喜欢这个资源，也可以顺手给本下载面板点个赞哦~)*"

            if file_results:
                await interaction.followup.send(content=f"🎁 验证通过！文件给你：\n{status_msg}{optional_tip}", files=self.make_discord_files(file_results), ephemeral=True)
                await self.send_dm_backup(interaction.user, file_results)
                self.record_download(interaction.user, row)
            else:
                await interaction.followup.send("❌ 文件下载失败（服务器无法获取源文件）。", ephemeral=True)

    def record_download(self, user, item_row):
        async def _update():
            async with get_db() as db:
                db.row_factory = aiosqlite.Row
                message_id = item_row['message_id']
                await db.execute("UPDATE protected_items SET download_count = download_count + 1 WHERE message_id = ?", (message_id,))
                file_data = json.loads(item_row['storage_urls'])
                filenames = json.dumps([f.get('filename','unknown') for f in file_data if isinstance(f, dict)])
                await db.execute("INSERT INTO download_log (user_id, message_id, title, filenames, timestamp) VALUES (?, ?, ?, ?, ?)", (user.id, message_id, item_row['title'], filenames, datetime.now(TZ_SHANGHAI).isoformat())); await db.commit()
        asyncio.create_task(_update())

    def get_requirement_text(unlock_type, password=None):
        mapping = {
            "like": "👍 需要 [点赞首楼]",
            "like_comment": "👍💬 需要 [点赞首楼 + 在帖子内发布新的评论（>5个字且非表情）]",
            "like_password": "👍🔐 需要 [点赞首楼 + 输入下载口令]",
            "like_comment_password": "👍💬🔐 需要 [点赞首楼 + 在帖子内发布新的评论（>5个字且非表情） + 下载口令]"
        }
        text = mapping.get(unlock_type, "未知条件")
        return text

class EphemeralDownloadView(ui.View):
    """在 /获取附件 命令中弹出的快捷视图"""
    def __init__(self, bot, items_rows):
        super().__init__(timeout=300)
        self.bot = bot
        # 为每个受保护项创建一个按钮
        for row in items_rows:
            btn = ui.Button(
                label=f"验证并获取: {row['title']}"[:80],
                style=discord.ButtonStyle.success,
                emoji="📥",
                custom_id=f"quick_dl_{row['message_id']}"
            )
            btn.callback = self.create_callback(row)
            self.add_item(btn)

    def create_callback(self, row):
        async def callback(interaction: discord.Interaction):
            dv = DownloadView(self.bot)
            await self.handle_direct_download(interaction, row)
        return callback

    async def handle_direct_download(self, interaction, row):
        # 这里提取了原 DownloadView.download_btn 的核心逻辑
        dv = DownloadView(self.bot)
        unlock_type = row['unlock_type']
        owner_id = row['owner_id']
        file_data = json.loads(row['storage_urls'])

        if "password" in unlock_type:
            # 权限检查（拥有者特权）
            has_test_role = False
            if isinstance(interaction.user, discord.Member) and interaction.user.get_role(TEST_ROLE_ID):
                has_test_role = True
            
            if interaction.user.id == owner_id and not has_test_role:
                await interaction.response.defer(ephemeral=True, thinking=True)
                file_results = await dv.fetch_files(file_data)
                if file_results:
                    await interaction.followup.send(content="👑 主人请拿好：", files=dv.make_discord_files(file_results), ephemeral=True)
                return
            
            # 普通用户弹出密码框
            await interaction.response.send_modal(PasswordUnlockModal(row['password'], row, dv, unlock_type))
        else:
            await interaction.response.defer(ephemeral=True, thinking=True)
            success, msg = await dv.check_requirements(interaction, unlock_type, owner_id)
            if not success:
                return await interaction.followup.send(msg, ephemeral=True)

            file_results = await dv.fetch_files(file_data)
            if file_results:
                dv.record_download(interaction.user, row)
                await interaction.followup.send(content="✅ 验证成功！文件已准备就绪：", files=dv.make_discord_files(file_results), ephemeral=True)
                await dv.send_dm_backup(interaction.user, file_results)
            else:
                await interaction.followup.send("❌ 文件下载失败，请联系作者。", ephemeral=True)
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
    async def cog_unload(self):
        self.bot.tree.remove_command(self.ctx_menu.name, type=self.ctx_menu.type)
    
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

    @app_commands.command(name="修复本频道面板", description="[管理员/作者] 刷新本频道所有旧面板，使其适配新逻辑")
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

    @app_commands.command(name="我今天下载了什么", description="查询今日下载历史和剩余次数")
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
                try:
                    filenames = ", ".join(json.loads(log['filenames']))
                except: filenames = "未知文件"
                ts = discord.utils.format_dt(datetime.fromisoformat(log['timestamp']), 'T')
                log_text += f"- **{log['title']}**: `{filenames}` ({ts})\n"
            embed.add_field(name="详细记录", value=log_text[:1024], inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="获取附件", description="获取本帖子里所有受保护的附件列表及下载入口")
    async def get_attachments(self, interaction: discord.Interaction):
        """原 /附件列表 的升级版"""
        await interaction.response.defer(ephemeral=True)
        
        # 获取本频道的受保护项
        posts = await self._get_active_posts(interaction.channel)
        
        if not posts:
            return await interaction.followup.send("🔍 当前位置没有发现受保护的附件。", ephemeral=True)

        embed = discord.Embed(
            title=f"📦 发现 {len(posts)} 组受保护附件",
            description="点击下方按钮验证条件并获取文件：",
            color=0xffb7c5
        )

        for post in posts[:10]: # 限制显示前10组，防止 Embed 过长
            try:
                files_info = json.loads(post['storage_urls'])
                file_list_str = "\n".join([f"📄 `{f.get('filename', '未知文件')}`" for f in files_info])
            except:
                file_list_str = "无法读取文件列表"

            req_text = get_requirement_text(post['unlock_type'], post['password'])
            
            embed.add_field(
                name=f"📌 {post['title']}",
                value=(
                    f"**文件内容：**\n{file_list_str}\n"
                    f"**获取条件：**\n{req_text}\n"
                    f"**累计下载：** `{post['download_count']}` 次\n"
                    f"**跳转原贴：** [点击此处](https://discord.com/channels/{interaction.guild_id}/{interaction.channel.id}/{post['message_id']})"
                ),
                inline=False
            )

        embed.set_footer(text="请确保您已满足上述条件后再点击获取按钮。")
        
        # 使用专门的快捷视图
        view = EphemeralDownloadView(self.bot, posts[:10])
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="管理附件", description="管理我发布的保护贴")
    async def manage_attachments(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        posts = await self._get_active_posts(interaction.channel, owner_id=interaction.user.id)
        if not posts: return await interaction.followup.send("你在这个频道还没有发过活跃的保护贴。", ephemeral=True)
        embed = discord.Embed(title=f"👑 {interaction.user.display_name} 的管理面板", color=0xffd700, description="这里列出了你在本频道发布的所有活跃保护贴。")
        for post in posts[:25]:
            ts = discord.utils.format_dt(datetime.fromisoformat(post['created_at']), 'R'); embed.add_field(name=f"📄 {post['title']}", value=f"下载: {post['download_count']}次 | 发布于: {ts}\n[🔗 点击跳转](https://discord.com/channels/{interaction.guild_id}/{interaction.channel.id}/{post['message_id']})", inline=False)
        options = [discord.SelectOption(label=p['title'][:50], description=f"ID: {p['message_id']}", value=str(p['message_id'])) for p in posts[:25]]
        select = ui.Select(placeholder="选择要删除的帖子...", options=options)
        async def callback(inter: discord.Interaction): await inter.response.send_message("确定要删除吗？", view=DeleteConfirmView(int(select.values[0])), ephemeral=True)
        select.callback = callback
        view = ui.View().add_item(select)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    async def convert_to_protected(self, interaction: discord.Interaction, message: discord.Message):
        if message.author != interaction.user: return await interaction.response.send_message("不可以动别人的东西！", ephemeral=True)
        if not message.attachments: return await interaction.response.send_message("消息里没有附件？", ephemeral=True)
        view = ProtectionDraftView(self.bot, interaction.user, message.attachments, target_message=message, default_log=message.content or None)
        embed = discord.Embed(title="🚀 正在启动保护向导...", color=0x87ceeb)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        await view.update_dashboard(interaction)

    @app_commands.command(name="设置附件保护", description="上传文件并创建保护贴")
    @app_commands.describe(file1="附件1", file2="附件2", file3="附件3", file4="附件4", file5="附件5", file6="附件6", file7="附件7", file8="附件8", file9="附件9", file10="附件10")
    async def create_protection(self, interaction: discord.Interaction, file1: discord.Attachment, file2: discord.Attachment=None, file3: discord.Attachment=None, file4: discord.Attachment=None, file5: discord.Attachment=None, file6: discord.Attachment=None, file7: discord.Attachment=None, file8: discord.Attachment=None, file9: discord.Attachment=None, file10: discord.Attachment=None):
        attachments = [f for f in [file1, file2, file3, file4, file5, file6, file7, file8, file9, file10] if f]
        if not attachments:
            return await interaction.response.send_message("请至少上传一个文件！", ephemeral=True)
        view = ProtectionDraftView(self.bot, interaction.user, attachments)
        embed = discord.Embed(title="🚀 正在启动保护向导...", color=0x87ceeb)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        await view.update_dashboard(interaction)

async def setup(bot):
    await bot.add_cog(ProtectionCog(bot))
