# cogs/protection/cog.py

import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime
import json
import asyncio

# --- 从新的地方导入 ---
from config import TZ_SHANGHAI, DAILY_DOWNLOAD_LIMIT
from . import db as protection_db
from .utils import is_valid_comment, extract_trace_from_bytes
from .views import ProtectionDraftView, PostListView, PostSelectionView, BumpButtonView


class ProtectionCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.ctx_menu = app_commands.ContextMenu(
            name="转为保护附件",
            callback=self.convert_to_protected
        )
        self.bot.tree.add_command(self.ctx_menu)
        self.bump_tasks = {}
        # 移除旧的 init_likes_db() 调用

    maker_group = app_commands.Group(name="贴主", description="[贴主] 附件保护发布与管理工具")
    user_group = app_commands.Group(name="保护附件", description="[用户] 下载与查询附件")
    admin_group = app_commands.Group(name="管理员专用", description="[管理] 系统维护工具")

    async def cog_load(self):
        self.bot.loop.create_task(self._restore_bump_tasks_after_ready())
        print("⏳ [ProtectionCog] 已安排置底任务恢复计划（将在 Bot 就绪后执行）...")

    async def _restore_bump_tasks_after_ready(self):
        await self.bot.wait_until_ready()
        print("🔄 [ProtectionCog] Bot 已就绪，开始恢复置底任务...")
        try:
            # 直接从数据库函数获取配置
            rows = await protection_db.get_all_bump_configs()
            count = 0
            for row in rows:
                channel_id = row['channel_id']
                channel = self.bot.get_channel(channel_id)
                if not channel:
                    try: channel = await self.bot.fetch_channel(channel_id)
                    except: pass

                if channel and (channel.id not in self.bump_tasks):
                    task = self.bot.loop.create_task(self._bump_loop(channel))
                    self.bump_tasks[channel_id] = task
                    count += 1
                elif not channel:
                     print(f"⚠️ [警告] 彻底无法找到频道 {channel_id}，跳过恢复。")

            print(f"✅ [ProtectionCog] 自动置底任务恢复完成！共恢复 {count} 个频道。")
        except Exception as e:
            print(f"❌ [ProtectionCog] 恢复任务失败: {e}")

    async def cog_unload(self):
        self.bot.tree.remove_command(self.ctx_menu.name, type=self.ctx_menu.type)
        for task in self.bump_tasks.values():
            task.cancel()

    # --- 监听器 ---
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.user_id != self.bot.user.id:
            await protection_db.add_like(payload.user_id, payload.message_id)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        await protection_db.remove_like(payload.user_id, payload.message_id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not isinstance(message.channel, discord.Thread):
            return

        if is_valid_comment(message.content):
            await protection_db.add_or_update_comment(message.author.id, message.channel.id, message.content)

        if message.attachments:
            is_owner = message.channel.owner_id == message.author.id
            if not is_owner and message.channel.owner_id is None:
                try:
                    thread = await message.guild.fetch_channel(message.channel.id)
                    is_owner = thread.owner_id == message.author.id
                except: pass

            if is_owner:
                # 触发旧版贴主附件汇总的逻辑
                asyncio.create_task(self.update_sticky_message(message.channel))


    # --- 管理员命令 ---
    @admin_group.command(name="修复面板", description="移除本频道所有旧面板的按钮")
    async def fix_panels(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        rows = await protection_db.get_items_in_channel(interaction.channel.id, limit=999) # 获取所有
        if not rows:
            return await interaction.followup.send("本频道在数据库中没有活跃记录。", ephemeral=True)

        success_count, fail_count = 0, 0
        for row in rows:
            try:
                msg = await interaction.channel.fetch_message(row['message_id'])
                await msg.edit(view=None)
                success_count += 1
                await asyncio.sleep(0.5)
            except:
                fail_count += 1
        await interaction.followup.send(f"✅ 修复完成！\n已移除按钮的消息: {success_count} 个\n失败/已删除: {fail_count} 个", ephemeral=True)

    @admin_group.command(name="溯源", description="检查文件水印并查询下载记录")
    @app_commands.describe(file="请上传需要检查的文件")
    async def trace_file(self, interaction: discord.Interaction, file: discord.Attachment):
        await interaction.response.defer(ephemeral=True)
        file_bytes = await file.read()
        trace_id = extract_trace_from_bytes(file_bytes, file.filename)

        if not trace_id:
            return await interaction.followup.send("⚠️ **未检测到溯源信息**\n文件可能非机器人分发，或水印已被破坏。", ephemeral=True)

        record = await protection_db.get_trace_record(trace_id)
        if not record:
            return await interaction.followup.send(f"⚠️ **检测到水印但数据丢失无法匹配！**\nTraceID: `{trace_id}`", ephemeral=True)

        user_text = f"<@{record['user_id']}> ({record['user_id']})"
        dl_time = discord.utils.format_dt(datetime.fromisoformat(record['created_at']))
        embed = discord.Embed(title="🔍 溯源报告", color=0xff0000, description=f"**文件**: `{record['filename']}`\n**溯源 ID**: `{trace_id}`")
        embed.add_field(name="👤 下载者", value=user_text, inline=False)
        embed.add_field(name="📅 下载时间", value=dl_time, inline=True)
        embed.add_field(name="📍 来源频道", value=f"<#{record['channel_id']}>", inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # --- 用户命令 ---
    @user_group.command(name="今日下载记录", description="查询今日下载历史和剩余次数")
    async def my_downloads_today(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        today_start_iso = datetime.now(TZ_SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        logs = await protection_db.get_user_downloads_since(interaction.user.id, today_start_iso)

        remaining = DAILY_DOWNLOAD_LIMIT - len(logs)
        embed = discord.Embed(title=f"📜 {interaction.user.display_name} 的今日下载记录", color=discord.Color.blue())
        embed.description = f"**今日下载次数**: {len(logs)}/{DAILY_DOWNLOAD_LIMIT}\n**剩余次数**: {remaining}"

        if logs:
            log_text = ""
            for log in logs:
                try: filenames = ", ".join(json.loads(log['filenames']))
                except: filenames = "未知"
                ts = discord.utils.format_dt(datetime.fromisoformat(log['timestamp']), 'T')
                log_text += f"- **{log['title']}**: `{filenames}` ({ts})\n"
            embed.add_field(name="详细记录", value=log_text[:1024], inline=False)
        else:
            embed.add_field(name="记录", value="今天还没有下载过任何附件哦。")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @user_group.command(name="获取附件", description="显示本频道最近的受保护附件列表")
    async def get_attachments_list(self, interaction: discord.Interaction):
        rows = await protection_db.get_items_in_channel(interaction.channel.id, limit=25) # 下拉菜单最多25个
        if not rows:
            return await interaction.response.send_message("❌ 本频道没有任何受保护的附件记录。", ephemeral=True)
        view = PostListView(self.bot, rows)
        embed = discord.Embed(title="📂 附件获取列表", description=f"发现本频道有 **{len(rows)}** 个最近的附件包。\n请在下方下拉菜单中选择一个下载。", color=0x87ceeb)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    # --- 贴主命令 ---
    @maker_group.command(name="管理附件", description="管理我在此讨论串中发布的附件")
    async def manage_attachments(self, interaction: discord.Interaction):
        if not isinstance(interaction.channel, (discord.Thread, discord.TextChannel)):
            return await interaction.response.send_message("❌  此命令只能在文字频道或论坛帖子中使用。", ephemeral=True)

        rows = await protection_db.get_user_items_in_channel(interaction.user.id, interaction.channel.id)
        if not rows:
            return await interaction.response.send_message("你在当前频道中没有发布过受保护的附件。", ephemeral=True)

        posts = [dict(row) for row in rows]
        view = PostSelectionView(posts)
        await interaction.response.send_message(f"你在此频道中发布了 {len(posts)} 个受保护的附件，请选择进行管理：", view=view, ephemeral=True)

    async def convert_to_protected(self, interaction: discord.Interaction, message: discord.Message):
        if message.author != interaction.user:
            return await interaction.response.send_message("不可以动别人的东西！", ephemeral=True)
        if not message.attachments:
            return await interaction.response.send_message("消息里没有附件？", ephemeral=True)

        view = ProtectionDraftView(self.bot, interaction.user, message.attachments, target_message=message, default_log=message.content or None)
        embed = discord.Embed(title="🚀 正在启动保护向导...", color=0x87ceeb)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        await view.update_dashboard(interaction)

    @maker_group.command(name="发布保护附件", description="上传文件并创建保护贴")
    async def create_protection(self, interaction: discord.Interaction, file1: discord.Attachment, file2: discord.Attachment=None, file3: discord.Attachment=None, file4: discord.Attachment=None, file5: discord.Attachment=None, file6: discord.Attachment=None, file7: discord.Attachment=None, file8: discord.Attachment=None, file9: discord.Attachment=None, file10: discord.Attachment=None):
        attachments = [f for f in [file1, file2, file3, file4, file5, file6, file7, file8, file9, file10] if f]
        if not attachments:
            return await interaction.response.send_message("请至少上传一个文件！", ephemeral=True)

        view = ProtectionDraftView(self.bot, interaction.user, attachments)
        embed = discord.Embed(title="🚀 正在启动保护向导...", color=0x87ceeb)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        await view.update_dashboard(interaction)

    @maker_group.command(name="置底附件列表", description="开启/关闭本频道附件列表的自动置底")
    @app_commands.describe(开关="'on'开启, 'off'关闭")
    async def auto_bump(self, interaction: discord.Interaction, 开关: str):
        is_admin = interaction.user.guild_permissions.administrator
        is_owner = isinstance(interaction.channel, discord.Thread) and interaction.channel.owner_id == interaction.user.id
        if not (is_admin or is_owner):
            return await interaction.response.send_message("❌ 只有 **管理员** 或 **贴主** 才能使用。", ephemeral=True)

        channel_id = interaction.channel.id
        if 开关.lower() == "off":
            if channel_id in self.bump_tasks:
                self.bump_tasks[channel_id].cancel()
                del self.bump_tasks[channel_id]
            await protection_db.remove_bump_config(channel_id)
            return await interaction.response.send_message("✅ 已**关闭**本频道的自动置底功能。", ephemeral=True)

        if 开关.lower() == "on":
            rows = await protection_db.get_items_in_channel(channel_id, limit=1)
            if not rows:
                return await interaction.response.send_message("❌ 本频道没有任何受保护的附件记录，无法开启置底。", ephemeral=True)

            if channel_id in self.bump_tasks: self.bump_tasks[channel_id].cancel()
            task = self.bot.loop.create_task(self._bump_loop(interaction.channel))
            self.bump_tasks[channel_id] = task
            await protection_db.add_bump_config(channel_id)
            await interaction.response.send_message(f"✅ **自动置底已开启**\nBot 将定时检查置底面板。", ephemeral=True)
        else:
            await interaction.response.send_message("请明确指示 `on` 或 `off`。", ephemeral=True)

    async def _bump_loop(self, channel):
        print(f"✅ [置底任务启动] 频道: {channel.name} ({channel.id})")
        try:
            while True:
                await asyncio.sleep(600) # 每10分钟检查一次
                try:
                    layout_data = BumpButtonView.create_layout(self.bot)
                    history_msgs = [msg async for msg in channel.history(limit=20)]
                    found_old_bump = None
                    is_latest = False

                    for i, msg in enumerate(history_msgs):
                        if msg.author.id == self.bot.user.id and msg.components and len(msg.components) > 0 and msg.components[0].children[0].custom_id == "bump_get_attachments":
                            found_old_bump = msg
                            if i == 0: is_latest = True
                            break # 只找最新的那一个面板

                    if found_old_bump and not is_latest:
                        await found_old_bump.delete()
                        await channel.send(**layout_data)
                    elif not found_old_bump:
                        await channel.send(**layout_data)

                except discord.Forbidden: break # 没权限了就退出
                except Exception as e: print(f"[{channel.id}] 置底任务出错: {e}")
        except asyncio.CancelledError:
            print(f"🛑 [置底任务停止] 频道: {channel.name}")

    async def update_sticky_message(self, thread: discord.Thread):
        """核心函数：扫描贴主附件 -> 发送或更新置底消息"""
        try:
            image_urls = []
            async for msg in thread.history(limit=None):
                if msg.author.id == thread.owner_id and msg.attachments:
                    image_urls.extend([att.url for att in msg.attachments if att.content_type and 'image' in att.content_type])
            if not image_urls: return
            image_urls.reverse()

            embed_desc = f"检测到新附件发布，已自动更新置底。\n当前共收录 **{len(image_urls)}** 个文件。\n\n"
            for i, url in enumerate(image_urls, 1):
                line = f"{i}. [附件链接]({url})\n"
                if len(embed_desc) + len(line) > 4000:
                    embed_desc += f"...还有 {len(image_urls) - i + 1} 个文件未显示"
                    break
                embed_desc += line
            embed = discord.Embed(title="📂 贴主附件汇总", description=embed_desc, color=0x2b2d31)

            old_msg_id = await protection_db.get_sticky_message_id(thread.id)
            if old_msg_id:
                try:
                    old_msg = await thread.fetch_message(old_msg_id)
                    await old_msg.edit(embed=embed)
                    return # 编辑完成，无需重发
                except discord.NotFound:
                    pass

            new_msg = await thread.send(embed=embed, silent=True)
            await protection_db.set_sticky_message(thread.id, new_msg.id)

        except Exception as e:
            print(f"❌ 执行贴主附件汇总更新时出错: {e}")