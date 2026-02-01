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

    maker_group = app_commands.Group(name="贴主", description="[贴主] 附件保护发布与管理工具")
    user_group = app_commands.Group(name="保护附件", description="[用户] 下载与查询附件")

    admin_group = app_commands.Group(name="管理员专用", description="[管理] 系统维护工具")

    async def cog_load(self):
        """
        插件加载时的钩子函数：
        1. 确保数据库表存在。
        2. 从数据库恢复所有已开启的置底任务。
        """
        print("🔄 [ProtectionCog] 正在初始化置底模块...")
        async with get_db() as db:
            # 1. 建表：记录哪些频道开启了置底 (简单存个 channel_id 就行)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS bump_config (
                    channel_id INTEGER PRIMARY KEY
                )
            """)
            await db.commit()

            # 2. 读取配置：查出所有开启了置底的频道
            cursor = await db.execute("SELECT channel_id FROM bump_config")
            rows = await cursor.fetchall()

        # 3. 恢复任务
        count = 0
        for row in rows:
            channel_id = row[0]
            # 获取频道对象（可能需要从缓存获取，如果缓存没有可能需要 fetch）
            channel = self.bot.get_channel(channel_id)

            # 如果缓存里没有（Bot刚启动可能还没同步完），尝试 fetch 或者忽略
            # 为了稳健，我们先检查是否只是 None
            if channel:
                # 启动循环任务（复用你的 _bump_loop）
                task = self.bot.loop.create_task(self._bump_loop(channel))
                self.bump_tasks[channel_id] = task
                count += 1
            else:
                print(f"⚠️ [警告] 无法恢复频道 {channel_id} 的置底任务（频道可能被删或Bot不可见）。")

        print(f"✅ [ProtectionCog] 初始化完成。已恢复 {count} 个频道的自动置底任务。")


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

        # 确保是在帖子(Thread)里
        if not isinstance(message.channel, discord.Thread):
            return

        # --- 逻辑 A: 原有的评论统计功能 ---
        if is_valid_comment(message.content):
            thread_id = message.channel.id
            async with get_db() as db:
                await db.execute("INSERT OR REPLACE INTO user_comments (user_id, message_id, content) VALUES (?, ?, ?)", (message.author.id, thread_id, message.content[:50]))
                await db.commit()

        # --- 逻辑 B: 新增的自动置底功能 ---
        # 只有当消息包含附件，才去检查是不是贴主
        if message.attachments:
            # 检查身份：是否是贴主本人
            is_owner = False
            if message.channel.owner_id == message.author.id:
                is_owner = True
            elif message.channel.owner_id is None:
                # 缓存缺失时的备用检查
                try:
                    thread = await message.guild.fetch_channel(message.channel.id)
                    if thread.owner_id == message.author.id:
                        is_owner = True
                except:
                    pass

            if is_owner:
                print(f"📥 [自动置底] 监测到贴主 {message.author} 在 {message.channel.name} 发布了新附件，正在执行置底...")
                asyncio.create_task(self.update_sticky_message(message.channel))

    
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

    @maker_group.command(name="置底附件", description="开启/关闭自动置底：检测到底部不是按钮时重发 (贴主/管理可用)")
    @app_commands.describe(original_message_id="填 off 关闭。不填则默认开启（无需指定特定ID）")
    async def auto_bump(self, interaction: discord.Interaction, original_message_id: str = None):
        """
        开启附件自动置底功能，并持久化保存配置。
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
            # A. 停止内存中的任务
            if channel.id in self.bump_tasks:
                self.bump_tasks[channel.id].cancel()
                del self.bump_tasks[channel.id]

            # B. 从数据库删除记录 (新增步骤)
            async with get_db() as db:
                await db.execute("DELETE FROM bump_config WHERE channel_id = ?", (channel.id,))
                await db.commit()

            return await interaction.response.send_message("✅ 已**关闭**本频道的自动置底功能（配置已保存）。", ephemeral=True)

        # --- 3. 开启功能的逻辑 ---
        # 只要有一条受保护记录即可开启
        async with get_db() as db:
            cursor = await db.execute("SELECT message_id FROM protected_items WHERE channel_id = ? LIMIT 1", (channel.id,))
            res = await cursor.fetchone()

        if not res:
            return await interaction.response.send_message("❌ 本频道没有任何受保护的附件记录，无法开启置底。", ephemeral=True)

        # A. 启动/重启内存任务
        if channel.id in self.bump_tasks:
            self.bump_tasks[channel.id].cancel() # 先停掉旧的防止双重运行

        task = self.bot.loop.create_task(self._bump_loop(channel))
        self.bump_tasks[channel.id] = task

        # B. 写入数据库 (新增步骤)
        async with get_db() as db:
            await db.execute("INSERT OR IGNORE INTO bump_config (channel_id) VALUES (?)", (channel.id,))
            await db.commit()

        await interaction.response.send_message(f"✅ **自动置底已开启**\nBot 将每 10 分钟检查一次。\n(配置已保存，Bot 重启后会自动恢复任务)", ephemeral=True)


    async def _bump_loop(self, channel):
        """
        后台循环任务：每10分钟检查一次
        具备【智能修复】功能：
        1. 搜索最近20条消息。
        2. 如果发现旧的面板且它是最新的 -> 原地复活（编辑修复按钮）。
        3. 如果旧面板被新消息压住了 -> 删旧发新（保持置底）。
        4. 没找到 -> 发新的。
        """
        from .ui.views import BumpButtonView

        print(f"✅ [置底任务启动] 频道: {channel.name} ({channel.id})")

        try:
            while True:
                try:
                    # 1. 准备好全新的界面数据（按钮是“活”的）
                    layout_data = BumpButtonView.create_layout(self.bot)
                    new_view = layout_data.get('view')

                    # 2. 扫描最近 20 条消息
                    history_msgs = [msg async for msg in channel.history(limit=20)]

                    found_old_bump = None
                    is_latest = False

                    for i, msg in enumerate(history_msgs):
                        if msg.author.id == self.bot.user.id and msg.components:
                            found_old_bump = msg
                            if i == 0:
                                is_latest = True

                    # 3. 决策逻辑
                    if found_old_bump:
                        if is_latest:
                            print(f"🔧 [自动修复] 在 {channel.name} 修复旧面板按钮...")
                            await found_old_bump.edit(**layout_data)
                        else:
                            try:
                                await found_old_bump.delete()
                            except:
                                pass
                            await channel.send(**layout_data)
                    else:
                        await channel.send(**layout_data)

                except discord.Forbidden:
                    print(f"[{channel.id}] 失去权限，停止置底循环。")
                    break
                except Exception as e:
                    print(f"[{channel.id}] 置底任务出错: {e}")
                    import traceback
                    traceback.print_exc()

                # 等待 10 分钟
                await asyncio.sleep(600)

        except asyncio.CancelledError:
            print(f"🛑 [置底任务停止] 频道: {channel.name}")

    async def update_sticky_message(self, thread: discord.Thread):
        """
        核心函数：扫描全贴附件 -> 删除旧置底 -> 发送新置底
        """
        try:
            image_data = []
            async for msg in thread.history(limit=None):
                if msg.author.id == thread.owner_id and msg.attachments:
                    for att in msg.attachments:
                        if att.content_type and (att.content_type.startswith('image/') or att.content_type.startswith('video/')):
                            image_data.append(att.url)

            if not image_data:
                return

            image_data.reverse()

            embed = discord.Embed(
                title="📂 贴主附件汇总",
                description=f"检测到新附件发布，已自动更新置底。\n当前共收录 **{len(image_data)}** 个文件。",
                color=0x2b2d31
            )

            content_str = ""
            for i, url in enumerate(image_data):
                line = f"{i+1}. [附件链接]({url})\n"
                if len(content_str) + len(line) > 3500: # 留点余量
                    content_str += f"...还有 {len(image_data) - i} 个文件未显示"
                    break
                content_str += line

            embed.description += "\n\n" + content_str

            async with get_db() as db:
                cursor = await db.execute("SELECT message_id FROM sticky_messages WHERE channel_id = ?", (thread.id,))
                row = await cursor.fetchone()

            if row:
                old_msg_id = row[0]
                try:
                    old_msg = await thread.fetch_message(old_msg_id)
                    await old_msg.delete()
                except discord.NotFound:
                    pass
                except Exception as e:
                    print(f"删除旧置底失败: {e}")

            new_msg = await thread.send(embed=embed, silent=True)

            async with get_db() as db:
                await db.execute("INSERT OR REPLACE INTO sticky_messages (channel_id, message_id) VALUES (?, ?)", (thread.id, new_msg.id))
                await db.commit()

        except Exception as e:
            print(f"❌ 执行置底更新时出错: {e}")


async def setup(bot):
    await bot.add_cog(ProtectionCog(bot))