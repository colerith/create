import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime
import json
import aiosqlite
import asyncio

from database import get_db
from .constants import TZ_SHANGHAI, DAILY_DOWNLOAD_LIMIT
from .db import init_likes_db
from .utils import is_valid_comment
from .utils import extract_trace_from_bytes
from .ui.views import ProtectionDraftView, PostListView, PostSelectionView

class ProtectionCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Context Menu
        self.ctx_menu = app_commands.ContextMenu(
            name="转为保护附件",
            callback=self.convert_to_protected
        )
        self.bot.tree.add_command(self.ctx_menu)
        self.bot.loop.create_task(init_likes_db())

    # 命令组定义
    maker_group = app_commands.Group(name="贴主", description="[贴主] 附件保护发布与管理工具")
    user_group = app_commands.Group(name="保护附件", description="[用户] 下载与查询附件")

    # 管理员组添加权限装饰器
    admin_group = app_commands.Group(name="管理员专用", description="[管理] 系统维护工具")

    async def cog_unload(self):
        self.bot.tree.remove_command(self.ctx_menu.name, type=self.ctx_menu.type)

    # --- 监听器 ---
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id: return
        async with get_db() as db:
            await db.execute("INSERT OR IGNORE INTO user_likes (user_id, message_id) VALUES (?, ?)", (payload.user_id, payload.message_id))
            await db.commit()

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        async with get_db() as db:
            await db.execute("DELETE FROM user_likes WHERE user_id = ? AND message_id = ?", (payload.user_id, payload.message_id))
            await db.commit()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot: return
        if isinstance(message.channel, discord.Thread) and is_valid_comment(message.content):
            thread_id = message.channel.id 
            async with get_db() as db:
                await db.execute("INSERT OR REPLACE INTO user_comments (user_id, message_id, content) VALUES (?, ?, ?)", (message.author.id, thread_id, message.content[:50]))
                await db.commit()

    # --- 管理员命令 ---
    @admin_group.command(name="修复面板", description="移除本频道所有旧面板的按钮（改用命令）")
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
                # 清除View，移除按钮
                await msg.edit(view=None)
                success_count += 1
                await asyncio.sleep(1.0) 
            except: fail_count += 1
        await interaction.followup.send(f"✅ 修复完成！\n已移除按钮的消息: {success_count} 个\n失败/已删除: {fail_count} 个", ephemeral=True)
    
    @admin_group.command(name="溯源", description="检查文件是否包含保护水印，并查询下载记录")
    @app_commands.describe(file="请上传需要检查的文件")
    async def trace_file(self, interaction: discord.Interaction, file: discord.Attachment):
        await interaction.response.defer(ephemeral=True) 

        # 1. 下载用户上传的文件
        try:
            file_bytes = await file.read()
        except:
            return await interaction.followup.send("❌ 文件读取失败，请检查网络或文件是否过大。", ephemeral=True)

        # 2. 提取特征码
        trace_id = extract_trace_from_bytes(file_bytes, file.filename)

        if not trace_id:
            return await interaction.followup.send("⚠️ **未检测到溯源信息**\n该文件可能不是由本机器人分发，或者水印已被破坏（如经过了格式转换、压缩或编辑）。", ephemeral=True)

        # 3. 只有拿到 ID，才去数据库查
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM file_traces WHERE trace_id = ?", (trace_id,))
            record = await cursor.fetchone()

        if not record:
            return await interaction.followup.send(f"⚠️ **检测到水印但由于数据丢失无法匹配！**\nTraceID: `{trace_id}`\n数据库中未找到该记录。", ephemeral=True)

        # 4. 生成详细报告
        downloader = interaction.guild.get_member(record['user_id'])
        user_text = f"{downloader.mention} ({record['user_id']})" if downloader else f"未知用户 ({record['user_id']})"

        dl_time = datetime.fromisoformat(record['created_at']).strftime('%Y-%m-%d %H:%M:%S')

        embed = discord.Embed(title="🔍 溯源报告", color=0xff0000)
        embed.description = f"**文件**: `{record['filename']}`\n**溯源 ID**: `{trace_id}`"

        embed.add_field(name="👤 下载者", value=user_text, inline=False)
        embed.add_field(name="📅 下载时间", value=dl_time, inline=True)
        embed.add_field(name="📍 来源服务器/频道", value=f"Guild: {record['guild_id']}\nChannel: <#{record['channel_id']}>", inline=True)
        embed.add_field(name="🔗 原始资源ID", value=f"`{record['message_id']}`", inline=False)

        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.set_footer(text="保护机制 · 幽灵追踪系统")

        await interaction.followup.send(embed=embed, ephemeral=True)

    # --- 用户命令 ---
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

    # --- 贴主命令 ---
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

import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime
import json
import aiosqlite
import asyncio

from database import get_db
from .constants import TZ_SHANGHAI, DAILY_DOWNLOAD_LIMIT
from .db import init_likes_db
from .utils import is_valid_comment
from .utils import extract_trace_from_bytes
from .ui.views import ProtectionDraftView, PostListView, PostSelectionView

class ProtectionCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Context Menu
        self.ctx_menu = app_commands.ContextMenu(
            name="转为保护附件",
            callback=self.convert_to_protected
        )
        self.bot.tree.add_command(self.ctx_menu)
        self.bot.loop.create_task(init_likes_db())

    # 命令组定义
    maker_group = app_commands.Group(name="贴主", description="[贴主] 附件保护发布与管理工具")
    user_group = app_commands.Group(name="保护附件", description="[用户] 下载与查询附件")

    # 管理员组添加权限装饰器
    admin_group = app_commands.Group(name="管理员专用", description="[管理] 系统维护工具")

    async def cog_unload(self):
        self.bot.tree.remove_command(self.ctx_menu.name, type=self.ctx_menu.type)

    # --- 监听器 ---
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id: return
        async with get_db() as db:
            await db.execute("INSERT OR IGNORE INTO user_likes (user_id, message_id) VALUES (?, ?)", (payload.user_id, payload.message_id))
            await db.commit()

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        async with get_db() as db:
            await db.execute("DELETE FROM user_likes WHERE user_id = ? AND message_id = ?", (payload.user_id, payload.message_id))
            await db.commit()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot: return
        if isinstance(message.channel, discord.Thread) and is_valid_comment(message.content):
            thread_id = message.channel.id 
            async with get_db() as db:
                await db.execute("INSERT OR REPLACE INTO user_comments (user_id, message_id, content) VALUES (?, ?, ?)", (message.author.id, thread_id, message.content[:50]))
                await db.commit()

    # --- 管理员命令 ---
    @admin_group.command(name="修复面板", description="移除本频道所有旧面板的按钮（改用命令）")
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
                # 清除View，移除按钮
                await msg.edit(view=None)
                success_count += 1
                await asyncio.sleep(1.0) 
            except: fail_count += 1
        await interaction.followup.send(f"✅ 修复完成！\n已移除按钮的消息: {success_count} 个\n失败/已删除: {fail_count} 个", ephemeral=True)
    
    @admin_group.command(name="溯源", description="检查文件是否包含保护水印，并查询下载记录")
    @app_commands.describe(file="请上传需要检查的文件")
    async def trace_file(self, interaction: discord.Interaction, file: discord.Attachment):
        await interaction.response.defer(ephemeral=True) 

        # 1. 下载用户上传的文件
        try:
            file_bytes = await file.read()
        except:
            return await interaction.followup.send("❌ 文件读取失败，请检查网络或文件是否过大。", ephemeral=True)

        # 2. 提取特征码
        trace_id = extract_trace_from_bytes(file_bytes, file.filename)

        if not trace_id:
            return await interaction.followup.send("⚠️ **未检测到溯源信息**\n该文件可能不是由本机器人分发，或者水印已被破坏（如经过了格式转换、压缩或编辑）。", ephemeral=True)

        # 3. 只有拿到 ID，才去数据库查
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM file_traces WHERE trace_id = ?", (trace_id,))
            record = await cursor.fetchone()

        if not record:
            return await interaction.followup.send(f"⚠️ **检测到水印但由于数据丢失无法匹配！**\nTraceID: `{trace_id}`\n数据库中未找到该记录。", ephemeral=True)

        # 4. 生成详细报告
        downloader = interaction.guild.get_member(record['user_id'])
        user_text = f"{downloader.mention} ({record['user_id']})" if downloader else f"未知用户 ({record['user_id']})"

        dl_time = datetime.fromisoformat(record['created_at']).strftime('%Y-%m-%d %H:%M:%S')

        embed = discord.Embed(title="🔍 溯源报告", color=0xff0000)
        embed.description = f"**文件**: `{record['filename']}`\n**溯源 ID**: `{trace_id}`"

        embed.add_field(name="👤 下载者", value=user_text, inline=False)
        embed.add_field(name="📅 下载时间", value=dl_time, inline=True)
        embed.add_field(name="📍 来源服务器/频道", value=f"Guild: {record['guild_id']}\nChannel: <#{record['channel_id']}>", inline=True)
        embed.add_field(name="🔗 原始资源ID", value=f"`{record['message_id']}`", inline=False)

        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.set_footer(text="保护机制 · 幽灵追踪系统")

        await interaction.followup.send(embed=embed, ephemeral=True)

    # --- 用户命令 ---
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

    # --- 贴主命令 ---
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
