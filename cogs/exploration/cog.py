#cogs/exploration/cog.py

import discord
from discord import app_commands, ui
from discord.ext import commands, tasks
from datetime import datetime, time
import asyncio
from zoneinfo import ZoneInfo

from config import EXPLORATION_TARGET_CHANNEL_IDS, EXPLORATION_ADMIN_USER_ID, TZ_SHANGHAI
from views import PaginatorView, SearchMethodView
from core.utils import TARGET_KEYWORDS

# ---核心搜索逻辑---
async def execute_search(interaction: discord.Interaction, search_type: str, query_data, selected_channels, selected_tag_ids=None):
    await interaction.response.send_message(("收到指令惹！正在全速启动搜索引擎... (0%)"), ephemeral=True)

    target_forums = []
    if selected_channels:
        for ch in selected_channels:
            full_channel = interaction.guild.get_channel(ch.id)
            if full_channel and isinstance(full_channel, discord.ForumChannel):
                target_forums.append(full_channel)
    else:
        target_forums = [ch for ch in interaction.guild.forums if isinstance(ch, discord.ForumChannel)]

    all_threads = []
    for forum in target_forums:
        all_threads.extend(forum.threads)

    if not all_threads:
        return await interaction.edit_original_response(content=chimidan_text("呜呜，当前范围内没有帖子可以搜捏..."))

    sem = asyncio.Semaphore(8)
    results = []
    processed_count = 0
    total_count = len(all_threads)
    target_tags_set = set(map(int, selected_tag_ids)) if selected_tag_ids else set()

    async def check_thread(thread):
        async with sem:
            try:
                if target_tags_set and not ({tag.id for tag in thread.applied_tags} & target_tags_set):
                    return None

                if search_type == "user":
                    if thread.owner_id == query_data.id: return thread
                elif search_type == "keyword":
                    keyword = query_data.lower()
                    if keyword in thread.name.lower(): return thread

                    starter = thread.starter_message or (await thread.history(limit=1, oldest_first=True).flatten())[0]
                    if starter and starter.content and keyword in starter.content.lower():
                        return thread
            except: pass
            return None

    tasks_list = [check_thread(t) for t in all_threads]
    last_update_time = datetime.now()

    for i, future in enumerate(asyncio.as_completed(tasks_list)):
        result = await future
        if result: results.append(result)

        now = datetime.now()
        if (now - last_update_time).total_seconds() > 1.5 or (i + 1) == total_count:
            percent = int(((i + 1) / total_count) * 100)
            try:
                await interaction.edit_original_response(
                    content=chimidan_text(f"正在全速搜索中...\n进度：{percent}% ({i+1}/{total_count})\n已找到：{len(results)} 个匹配")
                )
                last_update_time = now
            except discord.NotFound: break

    if not results:
        return await interaction.edit_original_response(content=chimidan_text(f"呜呜，翻遍了 {total_count} 个帖子也没找到捏..."))

    title = f"🔍 搜索结果: {len(results)}条" + (" (含标签筛选)" if selected_tag_ids else "")
    paginator = PaginatorView(results, title=title, is_daily=False)
    await interaction.edit_original_response(
        content=chimidan_text("搜索完成惹！找到以下内容："),
        embed=paginator.get_embed(),
        view=paginator
    )

class ExplorationCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.bot.add_view(SearchMethodView())
        self.daily_task.start()

    async def cog_unload(self):
        self.daily_task.cancel()

    async def get_todays_threads(self, guild):
        today_start = datetime.now(TZ_SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        threads_list = []
        for forum in guild.forums:
            if not forum.permissions_for(guild.me).read_messages: continue
            threads_list.extend([t for t in forum.threads if t.created_at.timestamp() >= today_start])
        threads_list.sort(key=lambda t: t.created_at, reverse=True)
        return threads_list

    async def refresh_channel_daily_panel(self, channel, resend=False):
        threads = await self.get_todays_threads(channel.guild)
        date_str = datetime.now(TZ_SHANGHAI).strftime('%Y年%m月%d日')
        panel_title = f"📅 {date_str} 更新日报"

        # 使用从 ui.views 导入的 PaginatorView
        view = PaginatorView(threads, title=panel_title, is_daily=True, tz_info=TZ_SHANGHAI)
        embed = view.get_embed()

        target_msg = None
        if not resend:
            try:
                async for msg in channel.history(limit=20):
                    if msg.author == self.bot.user and msg.embeds and "更新日报" in msg.embeds[0].title:
                        target_msg = msg
                        break
            except Exception as e: print(f"Error scanning channel {channel.id}: {e}")

        if target_msg:
            try: await target_msg.edit(embed=embed, view=view)
            except discord.NotFound: await channel.send(embed=embed, view=view) # 消息被删了，就重发
        else:
            if resend: # 如果是强制重发，先清理
                async for msg in channel.history(limit=20):
                     if msg.author == self.bot.user and msg.embeds and "更新日报" in msg.embeds[0].title:
                         await msg.delete(); await asyncio.sleep(0.5)
            await channel.send(embed=embed, view=view)

    @tasks.loop(minutes=10)
    async def daily_task(self):
        for channel_id in EXPLORATION_TARGET_CHANNEL_IDS:
            channel = self.bot.get_channel(channel_id)
            if channel: await self.refresh_channel_daily_panel(channel, resend=False)

    @daily_task.before_loop
    async def before_daily_task(self):
        await self.bot.wait_until_ready()

    # --- App Commands ---

    @app_commands.command(name="更新日报面板", description="[管理] 强制刷新并重发本频道的日报面板")
    async def manual_daily_report(self, interaction: discord.Interaction):
        if interaction.user.id != EXPLORATION_ADMIN_USER_ID and not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message(chimidan_text("你没有权限操作这个命令捏！"), ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        if interaction.channel_id in EXPLORATION_TARGET_CHANNEL_IDS:
            await self.refresh_channel_daily_panel(interaction.channel, resend=True)
            await interaction.followup.send(chimidan_text("日报面板已清理并发送最新版惹！"), ephemeral=True)
        else:
            threads = await self.get_todays_threads(interaction.guild)
            date_str = datetime.now(TZ_SHANGHAI).strftime('%Y-%m-%d')
            view = PaginatorView(threads, title=f"📅 {date_str} 日报 (预览)", is_daily=True, tz_info=TZ_SHANGHAI)
            await interaction.followup.send(embed=view.get_embed(), view=view, ephemeral=True)

    @app_commands.command(name="更新搜索面板", description="[管理] 清理旧面板并发送新的搜索面板")
    @app_commands.default_permissions(view_audit_log=True)
    async def refresh_search_panel(self, interaction: discord.Interaction):
        if interaction.user.id != EXPLORATION_ADMIN_USER_ID:
            return await interaction.response.send_message(chimidan_text("你没有权限操作这个命令捏！"), ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        channel = interaction.channel

        # 清理旧消息
        deleted_count = 0
        try:
            async for msg in channel.history(limit=50):
                if msg.author == self.bot.user and msg.embeds and msg.embeds[0].title == "🔍 奇米蛋搜索雷达":
                    await msg.delete(); deleted_count += 1; await asyncio.sleep(0.5)
        except Exception as e: print(f"Cleanup failed: {e}")

        embed = discord.Embed(title="🔍 奇米蛋搜索雷达", color=0x87ceeb)
        embed.description = chimidan_text("欢迎使用全服务器帖子搜索功能来捉！\n\n**使用指南：**\n...") # 省略部分文本
        embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        embed.set_footer(text="此面板永久有效，点击下方按钮即可使用")

        await channel.send(embed=embed, view=SearchMethodView())
        await interaction.followup.send(chimidan_text(f"处理完成！清理了 {deleted_count} 个旧面板，并发送了最新的搜索雷达！"), ephemeral=True)

    @app_commands.command(name="快捷搜索", description="调出快捷搜索面板")
    async def search_cmd(self, interaction: discord.Interaction):
        embed = discord.Embed(title="🔍 奇米蛋搜索雷达快捷版", description=chimidan_text("点击下方按钮开始搜索！"), color=0x87ceeb)
        await interaction.response.send_message(embed=embed, view=SearchMethodView(), ephemeral=True)
