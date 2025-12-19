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

# 确保 database.py 在同级目录
from database import get_db

TZ_SHANGHAI = ZoneInfo("Asia/Shanghai")
DAILY_DOWNLOAD_LIMIT = 50
TEST_ROLE_ID = 1402290127627091979

# --- Helper: Comment Validator ---
def is_valid_comment(content: str) -> bool:
    if not content: return False
    
    # 1. 禁止 Discord 表情代码 <a:name:id> 或 <:name:id>
    if re.search(r'<a?:.+?:\d+>', content):
        return False
        
    # 2. 去除链接、空格、换行
    content_clean = re.sub(r'http\S+', '', content).strip()
    content_clean = re.sub(r'\s+', '', content_clean) # 去除所有空白字符
    
    # 3. 基础长度检查 (>5)
    if len(content_clean) <= 5:
        return False

    # 4. 禁止纯数字/纯符号
    if content_clean.isdigit(): 
        return False
    
    # 5. 连续重复字符检查
    if re.search(r'(.)\1{4,}', content_clean):
        return False

    # 6. 字符多样性检查 (核心防刷逻辑)
    # 计算有多少种不同的字符。
    # "111111" -> 只有 '1' -> 1种
    # "ababab" -> 只有 'a','b' -> 2种
    # "可以可以" -> '可','以' -> 2种
    # "谢谢楼主分享" -> 6种 -> 通过
    # 阈值建议设为 4，意味着至少要有 4 个不同的字
    if len(set(content_clean)) < 4:
        return False
        
    return True

# --- Shared Logic Helpers (共用逻辑 - 移至全局) ---
# 这些函数必须在类定义之外，以便所有 View 和 Modal 都能调用

async def fetch_files_common(bot, file_data):
    """通用文件下载逻辑"""
    results = []
    if not isinstance(file_data, list): return []

    for item in file_data:
        if not isinstance(item, dict): continue
        download_url = item.get('url')
        
        # 尝试从引用消息更新链接
        if item.get('strategy') == 'msg_ref':
            try:
                channel = bot.get_channel(item['channel_id']) or await bot.fetch_channel(item['channel_id'])
                msg = await channel.fetch_message(item['message_id'])
                idx = item.get('attachment_index', 0)
                if 0 <= idx < len(msg.attachments):
                    download_url = msg.attachments[idx].url
            except: pass

        if not download_url: continue

        try:
            async with bot.http_session.get(download_url) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    if len(data) > 0:
                        results.append({'filename': item.get('filename', 'unknown'), 'bytes': data})
        except Exception as e: print(f"DL Error: {e}")
    return results

def make_discord_files_common(file_results):
    return [discord.File(io.BytesIO(res['bytes']), filename=res['filename']) for res in file_results]

async def send_dm_backup_common(user, file_results):
    files = make_discord_files_common(file_results)
    if not files: return
    try: await user.send(content="这是您刚刚下载的附件备份：", files=files)
    except: pass

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

async def check_requirements_common(interaction, unlock_type, owner_id, target_message_id):
    """通用验证逻辑：包含特权、每日限制、点赞、评论校验"""
    # 1. 身份特权检测
    has_test_role = isinstance(interaction.user, discord.Member) and interaction.user.get_role(TEST_ROLE_ID)
    is_owner = (interaction.user.id == owner_id)
    if is_owner and has_test_role: is_owner = False 
    if is_owner: return True, "owner"

    # 2. 每日下载限制
    today_start_iso = datetime.now(TZ_SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    async with get_db() as db:
        cursor = await db.execute("SELECT COUNT(*) FROM download_log WHERE user_id = ? AND timestamp >= ?", (interaction.user.id, today_start_iso))
        download_count = (await cursor.fetchone())[0]
    if download_count >= DAILY_DOWNLOAD_LIMIT:
        return False, f"⚠️ 您今日的下载次数已达上限（{DAILY_DOWNLOAD_LIMIT}/{DAILY_DOWNLOAD_LIMIT}）。"

    # =====================================================
    # 3. 定位【点赞目标】 (帖子首楼)
    # =====================================================
    op_msg = None
    
    # 尝试寻找帖子的首楼（第一条消息）
    if isinstance(interaction.channel, discord.Thread):
        try:
            if interaction.channel.starter_message:
                op_msg = interaction.channel.starter_message
            else:
                async for msg in interaction.channel.history(limit=1, oldest_first=True):
                    op_msg = msg
                    break
        except: pass

    if not op_msg:
        try:
            op_msg = await interaction.channel.fetch_message(target_message_id)
        except:
            return False, "❌ 无法定位原始帖子，请检查帖子是否已被删除。"

    # =====================================================
    # 4. 执行【点赞检测】 (针对 op_msg / 首楼)
    # =====================================================
    reacted = False
    for r in op_msg.reactions:
        async for u in r.users(limit=None): 
            if u.id == interaction.user.id: 
                reacted = True; break
        if reacted: break
    
    if not reacted:
        return False, f"🛑 您还没点赞呢！\n请点击这里跳转到 **[帖子首楼]({op_msg.jump_url})** 给作者点个赞吧！👍\n（点完赞后请再次点击按钮）"

    # =====================================================
    # 5. 执行【评论检测】 (针对 面板消息 之后的新评论)
    # =====================================================
    if "comment" in unlock_type:
        has_commented = False
        
        # 创建一个 Object 来代表面板消息
        panel_snowflake = discord.Object(id=target_message_id)

        try:
            # 扫描面板之后的消息
            async for msg in interaction.channel.history(after=panel_snowflake, limit=None):
                if msg.author.id == interaction.user.id:
                    if is_valid_comment(msg.content):
                        has_commented = True
                        break
        except Exception as e:
            print(f"Comment check error: {e}")
        
        if not has_commented:
            return False, (
                "💬 **评论未达标！**\n"
                "请在 **本下载面板下方** 发送一条有意义的新评论。\n"
                "❌ **拒绝以下内容**：\n"
                "- 字数过少 (需 >5 字)\n"
                "- 纯表情 / 纯数字 / 纯标点\n"
                "- 刷屏复读机 (如：啊啊啊啊、111111、顶顶顶)\n"
                "✅ **推荐**：说说你对这个资源的看法~"
            )

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
    
    def __init__(self, correct_password, item_row, bot, unlock_type): 
        super().__init__()
        self.c = correct_password
        self.row = item_row 
        self.bot = bot
        self.ut = unlock_type
    
    async def on_submit(self, i: discord.Interaction):
        if i.data['components'][0]['components'][0]['value'].strip() != self.c: 
            return await i.response.send_message("❌ 口令错误！", ephemeral=True)
        
        await i.response.defer(ephemeral=True, thinking=True)
        
        # 调用通用验证
        success, msg = await check_requirements_common(i, self.ut, self.row['owner_id'], self.row['message_id'])
        if not success:
            return await i.followup.send(msg, ephemeral=True)

        try: file_data = json.loads(self.row['storage_urls'])
        except: return await i.followup.send("❌ 数据损坏", ephemeral=True)

        # 调用通用下载
        file_results = await fetch_files_common(self.bot, file_data)
        if file_results: 
            await record_download_common(i.user, self.row)
            await i.followup.send(content="🔓 口令正确！文件给你：", files=make_discord_files_common(file_results), ephemeral=True)
            await send_dm_backup_common(i.user, file_results)
        else: 
            await i.followup.send("❌ 文件下载失败，请联系作者。", ephemeral=True)

# --- List View (For /获取附件 command) ---

class PostListView(ui.View):
    def __init__(self, bot, posts_rows):
        super().__init__(timeout=600)
        self.bot = bot
        self.posts = posts_rows # list of DB rows
        self.selected_row = None
        
        # 初始化下拉菜单
        options = []
        for p in self.posts:
            # 截断标题防止过长
            title = p['title'][:90]
            ts_str = datetime.fromisoformat(p['created_at']).strftime('%m-%d %H:%M')
            options.append(discord.SelectOption(
                label=title,
                description=f"发布于: {ts_str}",
                value=str(p['message_id']),
                emoji="📄"
            ))
        
        self.select_menu = ui.Select(placeholder="🔍 请选择要获取的附件...", options=options, row=0)
        self.select_menu.callback = self.on_select
        self.add_item(self.select_menu)

    async def on_select(self, interaction: discord.Interaction):
        # 获取用户选择的 message_id
        selected_id = int(self.select_menu.values[0])
        self.selected_row = next((p for p in self.posts if p['message_id'] == selected_id), None)
        
        if not self.selected_row:
            return await interaction.response.send_message("选择出错，请重试。", ephemeral=True)

        # 更新按钮状态
        self.btn_download.disabled = False
        
        # 构建详情 Embed
        try:
            file_data = json.loads(self.selected_row['storage_urls'])
            file_list = "\n".join([f"📄 {f.get('filename','???')}" for f in file_data])
        except: file_list = "解析错误"
        
        mode_map = {
            "like": "👍 点赞首楼", 
            "like_comment": "👍💬 点赞 + 评论 (>5字，禁表情)", 
            "like_password": "👍🔐 点赞 + 口令", 
            "like_comment_password": "👍💬🔐 点赞 + 评论 + 口令"
        }
        
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
        
        # 密码模式 -> 弹窗
        if "password" in unlock_type:
            # 列表模式下也允许 owner 直接下载，如不需要可删掉下面几行
            has_test_role = isinstance(interaction.user, discord.Member) and interaction.user.get_role(TEST_ROLE_ID)
            if interaction.user.id == row['owner_id'] and not has_test_role:
                 await interaction.response.defer(ephemeral=True, thinking=True)
                 file_data = json.loads(row['storage_urls'])
                 file_results = await fetch_files_common(self.bot, file_data)
                 if file_results: await interaction.followup.send(content="👑 主人请拿好：", files=make_discord_files_common(file_results), ephemeral=True)
                 return

            await interaction.response.send_modal(PasswordUnlockModal(row['password'], row, self.bot, unlock_type))
        # 非密码模式
        else:
            await interaction.response.defer(ephemeral=True, thinking=True)
            success, msg = await check_requirements_common(interaction, unlock_type, row['owner_id'], row['message_id'])
            if not success: return await interaction.followup.send(msg, ephemeral=True)
            
            file_data = json.loads(row['storage_urls'])
            file_results = await fetch_files_common(self.bot, file_data)
            
            # 计算剩余
            today_start_iso = datetime.now(TZ_SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            async with get_db() as db:
                cursor = await db.execute("SELECT COUNT(*) FROM download_log WHERE user_id = ? AND timestamp >= ?", (interaction.user.id, today_start_iso))
                cnt = (await cursor.fetchone())[0]

            if file_results:
                await interaction.followup.send(content=f"🎁 验证通过！\n今日剩余: {DAILY_DOWNLOAD_LIMIT - cnt - 1}/{DAILY_DOWNLOAD_LIMIT}", files=make_discord_files_common(file_results), ephemeral=True)
                await send_dm_backup_common(interaction.user, file_results)
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
            
            if not row:
                return await interaction.response.send_message("❌ 该附件已被作者删除或失效。", ephemeral=True)
            
            file_data = json.loads(row['storage_urls'])
            unlock_type = row['unlock_type']
            owner_id = row['owner_id']

        # 密码模式 -> 弹窗
        if "password" in unlock_type:
            has_test_role = isinstance(interaction.user, discord.Member) and interaction.user.get_role(TEST_ROLE_ID)
            if interaction.user.id == owner_id and not has_test_role:
                # 拥有者直接下载
                await interaction.response.defer(ephemeral=True, thinking=True)
                file_results = await fetch_files_common(self.bot, file_data)
                if file_results: await interaction.followup.send(content="👑 主人请拿好：", files=make_discord_files_common(file_results), ephemeral=True)
                return

            await interaction.response.send_modal(PasswordUnlockModal(row['password'], row, self.bot, unlock_type))
        
        # 非密码模式 -> 直接验证
        else:
            await interaction.response.defer(ephemeral=True, thinking=True)
            success, msg = await check_requirements_common(interaction, unlock_type, owner_id, message_id)
            if not success: return await interaction.followup.send(msg, ephemeral=True)

            file_results = await fetch_files_common(self.bot, file_data)
            
            # 计算剩余
            today_start_iso = datetime.now(TZ_SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            async with get_db() as db:
                cursor = await db.execute("SELECT COUNT(*) FROM download_log WHERE user_id = ? AND timestamp >= ?", (interaction.user.id, today_start_iso))
                cnt = (await cursor.fetchone())[0]
            
            if file_results:
                await interaction.followup.send(content=f"🎁 验证通过！\n今日剩余: {DAILY_DOWNLOAD_LIMIT - cnt - 1}/{DAILY_DOWNLOAD_LIMIT}", files=make_discord_files_common(file_results), ephemeral=True)
                await send_dm_backup_common(interaction.user, file_results)
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

    @app_commands.command(name="获取附件", description="显示本频道最近的5个受保护附件列表")
    async def get_attachments_list(self, interaction: discord.Interaction):
        # 1. 查询最近 5 条记录
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM protected_items WHERE channel_id = ? ORDER BY created_at DESC LIMIT 5", 
                (interaction.channel.id,)
            )
            rows = await cursor.fetchall()

        if not rows:
            return await interaction.response.send_message("❌ 本频道没有任何受保护的附件记录。", ephemeral=True)

        # 2. 如果只有1条，也用下拉菜单（保持一致性），或者你可以选择直接显示
        view = PostListView(self.bot, rows)
        
        embed = discord.Embed(title="📂 附件获取列表", description=f"发现本频道有 **{len(rows)}** 个最近的附件包。\n请在下方下拉菜单中选择一个进行查看和下载。", color=0x87ceeb)
        
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

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
