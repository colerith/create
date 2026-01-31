import discord
from discord import ui
import json
import io
import asyncio
import os
import aiosqlite
from datetime import datetime
from database import get_db
from ..constants import TZ_SHANGHAI, BACKUP_CHANNEL_ID, DAILY_DOWNLOAD_LIMIT, TEST_ROLE_ID

# 引入我们新写的“魔法工具”
from ..utils import (
    fetch_files_common,
    make_discord_files_common,
    check_requirements_common,
    record_download_common,
    inject_smart_trace,  # 新增：注入指纹
    generate_trace_id    # 新增：生成ID
)
# 引入新写的数据库记录函数
from ..db import log_file_trace
from .modals import DraftTitleModal, DraftNoteModal, DraftPasswordModal, RenameFileModal, PasswordUnlockModal

# --- 延迟下载视图 (核心修改区域) ---
class AuthorNoteView(ui.View):
    def __init__(self, bot, row):
        super().__init__(timeout=300)
        self.bot = bot
        self.row = row
        self.downloaded = False

    @ui.button(label="⏳ 请阅读说明 (5s)", style=discord.ButtonStyle.secondary, disabled=True)
    async def btn_confirm(self, interaction: discord.Interaction, button: ui.Button):

        if self.downloaded: return
        self.downloaded = True

        # 禁用按钮防止重复点击，并更新提示
        button.disabled = True
        button.label = "🔍 正在处理溯源..."
        await interaction.response.edit_message(view=self)

        # 下载逻辑
        try:
            # 1. 获取原始文件数据 (这里面存着我们改好的正确文件名！)
            file_data = json.loads(self.row['storage_urls'])

            # 2. 下载文件内容 (这里返回的文件名往往是被Discord处理过的乱码或下划线)
            raw_results = await fetch_files_common(self.bot, file_data)

            # 3. 记录普通下载日志
            await record_download_common(interaction.user, self.row)

            if raw_results:
                final_files_to_send = []
                guild_id = interaction.guild_id if interaction.guild else 0
                channel_id = interaction.channel_id
                timestamp = datetime.now(TZ_SHANGHAI).isoformat()

                # --- 核心溯源注入逻辑 ---
                # 修改点：使用了 enumerate 来同时获取索引 i
                # 这样我们就能通过 file_data[i]['filename'] 拿到正确的中文名
                for i, res in enumerate(raw_results):
                    # 尝试获取正确的显示文件名，如果拿不到就只好用原始的
                    correct_filename = file_data[i].get('filename', res['filename'])

                    # A. 生成唯一追踪码
                    trace_id = generate_trace_id()

                    # B. 注入指纹
                    # 注意：这里我们依然传 correct_filename 进去，以便注入工具能正确识别文件类型
                    new_bytes = inject_smart_trace(
                        res['bytes'],
                        correct_filename,
                        trace_id
                    )

                    # C. 记录到溯源数据库
                    await log_file_trace(
                        trace_id=trace_id,
                        user_id=interaction.user.id,
                        guild_id=guild_id,
                        channel_id=channel_id,
                        message_id=self.row['message_id'],
                        filename=correct_filename, # 记录里也存正确名字
                        timestamp=timestamp
                    )

                    # D. 构建发送用的文件对象
                    # 关键修改：这里强制使用 correct_filename 作为发送给用户的文件名！
                    final_files_to_send.append(
                        discord.File(io.BytesIO(new_bytes), filename=correct_filename)
                    )
                # -----------------------

                # 计算剩余额度 (这一块保持不变)
                today_start_iso = datetime.now(TZ_SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
                async with get_db() as db:
                    cursor = await db.execute("SELECT COUNT(*) FROM download_log WHERE user_id = ? AND timestamp >= ?", (interaction.user.id, today_start_iso))
                    cnt = (await cursor.fetchone())[0]

                await interaction.followup.send(
                    content=f"✅ **获取成功！**\n🛡️ 本文件包含溯源指纹，请勿私自转传。\n今日剩余额度: {DAILY_DOWNLOAD_LIMIT - cnt}/{DAILY_DOWNLOAD_LIMIT}\n请查收下方附件：",
                    files=final_files_to_send,
                    ephemeral=True
                )
            else:
                await interaction.followup.send("❌ 文件数据读取失败，请联系管理员。", ephemeral=True)
        except Exception as e:
            import traceback
            traceback.print_exc()
            await interaction.followup.send(f"❌ 发生未知错误: {e}", ephemeral=True)


async def start_download_flow(interaction: discord.Interaction, bot, row):
    """
    通用下载流程控制：
    1. 显示作者提示 Embed
    2. 等待 5 秒
    3. 启用按钮供用户点击下载
    """
    author_note = row['log'] if row['log'] else "（作者未留下额外说明）"

    embed = discord.Embed(title="📝 作者提示", description=author_note, color=0xffd700)
    embed.set_footer(text="请仔细阅读以上内容，5秒后可点击下方按钮获取文件。")

    view = AuthorNoteView(bot, row)

    # 发送消息（按钮初始是禁用的）
    if interaction.response.is_done():
        msg = await interaction.followup.send(embed=embed, view=view, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        msg = await interaction.original_response()

    # 延时 5 秒
    await asyncio.sleep(5)

    # 更新按钮状态
    view.children[0].disabled = False
    view.children[0].label = "✅ 我已阅读，获取附件"
    view.children[0].style = discord.ButtonStyle.success
    try:
        await msg.edit(view=view)
    except:
        pass # 用户可能已经关闭了弹窗

# --- 自定义文件名选择视图 ---
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

# --- 草稿/发布视图 ---
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
            # 优先私信，失败转存备份频道
            try:
                dm = await self.user.create_dm()
                backup_msg = await dm.send(content=f"【{self.draft_title}】的私信备份！\nID: {interaction.id}\n(此消息仅作为文件源，请勿删除)", files=files_to_send)
            except:
                # Fallback
                fallback_channel = self.bot.get_channel(BACKUP_CHANNEL_ID)
                if not fallback_channel: fallback_channel = await self.bot.fetch_channel(BACKUP_CHANNEL_ID)
                backup_msg = await fallback_channel.send(content=f"📦 **备用存储** (DM Failed)\nUser: {self.user} ({self.user.id})\nTitle: {self.draft_title}", files=files_to_send)

            for i, att in enumerate(backup_msg.attachments):
                real_display_name = self.custom_names.get(i, self.attachments[i].filename)
                stored_data.append({
                    "strategy": "msg_ref", "channel_id": backup_msg.channel.id, "message_id": backup_msg.id,
                    "attachment_index": i, "filename": real_display_name, "url": att.url
                })
        except Exception as e: return await interaction.followup.send(f"备份发送失败：{e}", ephemeral=True)

        if self.target_message:
            try: await self.target_message.delete()
            except: pass

        final_desc = "📋 **本附件受保护，请按照下方指示获取。**"
        embed = discord.Embed(title=f"✨ {self.draft_title}", description=final_desc, color=discord.Color.from_rgb(255, 183, 197))
        embed.set_author(name=f"由 {self.user.display_name} 发布", icon_url=self.user.display_avatar.url)
        mode_map = {"like": "👍 **点赞首楼**", "like_comment": "👍💬 **点赞首楼 + 回复本贴**", "like_password": "👍🔐 **点赞首楼 + 口令**", "like_comment_password": "👍💬🔐 **点赞首楼 + 回复本贴 + 口令**"}
        embed.add_field(name="🔑 获取条件", value=mode_map.get(self.draft_mode, "未知"), inline=True)
        embed.add_field(name="📦 文件数量", value=f"**{len(stored_data)}** 个", inline=True)
        now_ts = discord.utils.format_dt(datetime.now(TZ_SHANGHAI))
        embed.add_field(name="⏰ 发布时间", value=now_ts, inline=True)

        embed.add_field(name="📥 如何下载？", value="请使用命令：\n**`/保护附件 获取附件`**\n来验证条件并下载文件。", inline=False)
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

        await interaction.followup.send("✅ 发布成功！已移除直接获取按钮，引导用户使用命令。", ephemeral=True)

# --- 帖子列表视图 ---
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

        # 1. 检查密码模式
        if "password" in unlock_type:
            has_test_role = isinstance(interaction.user, discord.Member) and interaction.user.get_role(TEST_ROLE_ID)
            # 如果是主人且没测试身份，直接给文件（跳过Delay，但建议还是打上水印以防万一）
            # 不过为了方便自己测试，保持原样逻辑给无水印版也行，这里我为了你的测试体验，保持了主人直通，但如果你希望测试水印功能，建议用小号下载
            if interaction.user.id == row['owner_id'] and not has_test_role:
                 await interaction.response.defer(ephemeral=True, thinking=True)
                 file_data = json.loads(row['storage_urls'])
                 # 依然是原始文件
                 file_results = await fetch_files_common(self.bot, file_data)
                 if file_results: await interaction.followup.send(content="👑 主人请拿好（无水印原版）：", files=make_discord_files_common(file_results), ephemeral=True)
                 return
            # 否则弹出密码框
            await interaction.response.send_modal(PasswordUnlockModal(row['password'], row, self.bot, unlock_type))

        # 2. 普通模式 (验证点赞评论)
        else:
            await interaction.response.defer(ephemeral=True, thinking=True)
            success, msg = await check_requirements_common(interaction, unlock_type, row['owner_id'], row['message_id'])
            if not success: return await interaction.followup.send(msg, ephemeral=True)

            # 验证通过后，进入注入流程
            await start_download_flow(interaction, self.bot, row)

# 请添加到 cogs/protection/ui/views.py 中

class BumpButtonView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None) # 持久化视图，永不仅用
        self.bot = bot

    @classmethod
    def create_layout(cls, bot, origin_id=None):
        """
        静态方法：生成发送置底消息时所需的参数字典。
        origin_id 在这里甚至不需要用到，因为我们只是唤起通用的频道列表。
        """
        view = cls(bot)
        embed = discord.Embed(
            description="⬇️ **本频道有受保护的附件** ⬇️\n为了防止资源被聊天记录淹没，请点击下方按钮查看下载列表。",
            color=0x2b2d31
        )
        return {"embed": embed, "view": view}

    @discord.ui.button(label="获取本帖附件", style=discord.ButtonStyle.blurple, custom_id="bump_get_attachments", emoji="📥")
    async def get_attachments_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 核心逻辑：复刻 /保护附件 获取附件 的功能
        # 1. 查库
        from database import get_db # 需确保能导入

        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            # 查找本频道最近的 5 条记录
            cursor = await db.execute(
                "SELECT * FROM protected_items WHERE channel_id = ? ORDER BY created_at DESC LIMIT 5",
                (interaction.channel.id,)
            )
            rows = await cursor.fetchall()

        if not rows:
            return await interaction.response.send_message("❌ 本频道当前没有任何受保护的附件记录。", ephemeral=True)

        # 2. 生成那个带下拉菜单的 View (PostListView)
        view = PostListView(self.bot, rows)

        embed = discord.Embed(
            title="📂 附件获取列表",
            description=f"发现本频道有 **{len(rows)}** 个最近的附件包。\n请在下方下拉菜单中选择一个进行查看和下载。",
            color=0x87ceeb
        )

        # 3. 发送给用户（仅自己可见）
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
