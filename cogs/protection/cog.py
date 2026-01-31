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
        self.ctx_menu = app_commands.ContextMenu(
            name="转为保护附件",
            callback=self.convert_to_protected
        )
        self.bot.tree.add_command(self.ctx_menu)
        self.bot.loop.create_task(init_likes_db())
        self.bump_tasks = {}

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

    @maker_group.command(name="置底附件", description="每10分钟检查附件按钮是否在底部，被顶走则重发 (仅贴主/管理可用)")
    @app_commands.describe(original_message_id="包含附件数据的原始消息ID (如果不填则尝试自动查找)")
    async def auto_bump(self, interaction: discord.Interaction, original_message_id: str = None):
        """
        开启附件自动置底功能。输入 off 可以关闭当前频道的置底。
        """
        channel = interaction.channel

        # --- 1. 权限检查 ---
        is_admin = interaction.user.guild_permissions.administrator
        is_owner = False
        if isinstance(channel, discord.Thread) and channel.owner_id == interaction.user.id:
            is_owner = True

        if not (is_admin or is_owner):
            return await interaction.response.send_message("❌ 只有 **管理员** 或 **贴主** 才能使用此功能。", ephemeral=True)

        # --- 2. 关闭功能的逻辑 ---
        if original_message_id and original_message_id.lower() == "off":
            if channel.id in self.bump_tasks:
                self.bump_tasks[channel.id].cancel() # 取消任务
                del self.bump_tasks[channel.id]
                return await interaction.response.send_message("✅ 已**关闭**本频道的自动置底功能。", ephemeral=True)
            else:
                return await interaction.response.send_message("⚠️ 本频道当前没有开启自动置底。", ephemeral=True)

        # --- 3. 确定原始消息 ID ---
        target_msg_id = None
        if original_message_id:
            try:
                target_msg_id = int(original_message_id)
            except:
                return await interaction.response.send_message("❌ 消息 ID 格式错误。", ephemeral=True)
        else:
            # 自动查找逻辑：在数据库里找属于这个频道且有下载数据的最新一条消息
            async with get_db() as db:
                cursor = await db.execute("SELECT message_id FROM download_rules WHERE channel_id = ? ORDER BY timestamp DESC LIMIT 1", (channel.id,))
                res = await cursor.fetchone()
                if res:
                    target_msg_id = res[0]

        if not target_msg_id:
            return await interaction.response.send_message("❌ 找不到有效的文件记录，请手动输入 `original_message_id`。", ephemeral=True)

        # --- 4. 启动任务 ---
        # 如果已经有任务在运行，先停掉旧的
        if channel.id in self.bump_tasks:
            self.bump_tasks[channel.id].cancel()

        # 创建后台任务
        task = asyncio.create_task(self._bump_loop(channel, target_msg_id))
        self.bump_tasks[channel.id] = task

        await interaction.response.send_message(f"✅ **自动置底已开启**\n关联源消息: `{target_msg_id}`\n机制: 每 10 分钟检查一次，若不是最新消息则重发。", ephemeral=True)

    async def _bump_loop(self, channel, origin_id):
        """
        后台循环任务：每10分钟检查一次
        """
        # 只需要导入 View 类即可，不需要在这里写长长的文案了
        from .ui.views import BumpButtonView

        last_bump_msg = None

        try:
            while True:
                try:
                    # 1. 检查最新消息
                    messages = [msg async for msg in channel.history(limit=1)]
                    latest_msg = messages[0] if messages else None

                    need_resend = False

                    if not latest_msg:
                        need_resend = True
                    elif last_bump_msg and latest_msg.id == last_bump_msg.id:
                        need_resend = False
                    else:
                        need_resend = True

                    if need_resend:
                        # 删除旧消息
                        if last_bump_msg:
                            try:
                                await last_bump_msg.delete()
                            except (discord.NotFound, discord.Forbidden):
                                pass

                        # --- 核心变化：直接调用 View 的类方法获取发送参数 ---
                        # 这行代码现在的含义是：给我一套“BumpButtonView”的标准外观参数
                        send_kwargs = BumpButtonView.create_layout(self.bot, origin_id)

                        # 使用 ** 解包字典，自动填入 content 和 view
                        last_bump_msg = await channel.send(**send_kwargs)

                except discord.Forbidden:
                    print(f"[{channel.id}] 失去权限，停止置底。")
                    break
                except Exception as e:
                    print(f"[{channel.id}] 置底任务出错: {e}")

                # 等待 10 分钟
                await asyncio.sleep(600)

        except asyncio.CancelledError:
            pass

async def setup(bot):
    await bot.add_cog(ProtectionCog(bot))
