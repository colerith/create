import discord
from discord import ui
import os
import json
import asyncio
from ..utils import check_requirements_common

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

class PasswordUnlockModal(ui.Modal, title="请输入口令"):
    password_input = ui.TextInput(label="口令", placeholder="请输入...", max_length=50)
    def __init__(self, correct_password, item_row, bot, unlock_type): 
        super().__init__()
        self.c = correct_password
        self.row = item_row 
        self.bot = bot
        self.ut = unlock_type
    
    async def on_submit(self, i: discord.Interaction):
        user_input = self.password_input.value.strip()
        if user_input != self.c:
            return await i.response.send_message("❌ 口令错误！", ephemeral=True)

        await i.response.defer(ephemeral=True, thinking=True)
        success, msg = await check_requirements_common(i, self.ut, self.row['owner_id'], self.row['message_id'])
        if not success: return await i.followup.send(msg, ephemeral=True)

        # 【关键修复】在此处延迟导入，解决循环依赖
        from .views import start_download_flow
        await start_download_flow(i, self.bot, self.row)
