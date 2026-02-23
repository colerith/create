# cogs/exploration/cog.py

import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime
import asyncio

from config import EXPLORATION_TARGET_CHANNEL_IDS, ADMIN_USER_ID, TZ_SHANGHAI
from .views import SearchMethodView, PaginatorView
from ..core.db import get_panel_message_id, set_panel_message_id, remove_panel_record

class ExplorationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.add_view(SearchMethodView())
        self.daily_task.start()

    async def cog_unload(self):
        self.daily_task.cancel()

    async def get_todays_threads(self, guild: discord.Guild) -> list[discord.Thread]:
        """获取今天发布的所有帖子（此函数无需修改）"""
        today_start_ts = datetime.now(TZ_SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        threads_list = []
        for forum in guild.forums:
            if not forum.permissions_for(guild.me).read_messages:
                continue
            threads_list.extend([t for t in forum.threads if t.created_at.timestamp() >= today_start_ts])
        threads_list.sort(key=lambda t: t.created_at, reverse=True)
        return threads_list

    async def refresh_channel_daily_panel(self, channel: discord.TextChannel, resend: bool = False):
        """[数据库驱动版] 刷新每日更新面板"""
        threads = await self.get_todays_threads(channel.guild)
        date_str = datetime.now(TZ_SHANGHAI).strftime('%Y年%m月%d日')
        panel_title = f"📅 {date_str} 更新日报"
        view = PaginatorView(threads, title=panel_title, is_daily=True)
        embed = view.get_embed()

        # 1. 从数据库精准获取 message_id
        message_id = await get_panel_message_id(channel.id, 'daily_report')
        target_msg = None

        if message_id and not resend:
            try:
                target_msg = await channel.fetch_message(message_id)
                await target_msg.edit(embed=embed, view=view)
                return  # 精准编辑成功，任务完成
            except discord.NotFound:
                # 消息被删了，从数据库移除记录，之后会重新发送
                await remove_panel_record(channel.id, 'daily_report')
            except Exception as e:
                print(f"编辑数据库记录的日报面板({message_id})失败: {e}")
                # 编辑失败，同样走重发逻辑

        # 2. 发送新面板的逻辑 (找不到记录/编辑失败/强制重发)
        # 如果是强制重发，先尝试删除旧的
        if resend and message_id:
            try:
                old_msg = await channel.fetch_message(message_id)
                await old_msg.delete()
            except discord.NotFound:
                pass  # 消息本就不在了
            finally:
                # 无论如何都清除数据库记录，确保之后写入的是新的
                await remove_panel_record(channel.id, 'daily_report')

        # 为了更干净，重发时也清理一下历史记录，防止意外残留
        if resend:
            try:
                async for msg in channel.history(limit=20):
                     if msg.author == self.bot.user and msg.embeds and "更新日报" in msg.embeds[0].title:
                         await msg.delete()
            except Exception: pass

        # 发送新的面板
        new_msg = await channel.send(embed=embed, view=view)
        # 将新面板的 ID 记录到数据库
        await set_panel_message_id(channel.id, new_msg.id, 'daily_report')


    @tasks.loop(minutes=10)
    async def daily_task(self):
        """自动任务：调用数据库驱动的刷新逻辑"""
        for channel_id in EXPLORATION_TARGET_CHANNEL_IDS:
            if channel := self.bot.get_channel(channel_id):
                # 使用非强制重发模式进行常规更新
                await self.refresh_channel_daily_panel(channel, resend=False)

    @daily_task.before_loop
    async def before_daily_task(self):
        await self.bot.wait_until_ready()

    # --- 【核心修改】更新手动命令 ---
    @app_commands.command(name="更新日报面板", description="[管理] 强制刷新并重发本频道的日报面板")
    @app_commands.default_permissions(administrator=True)
    async def manual_daily_report(self, interaction: discord.Interaction):
        if interaction.user.id != ADMIN_USER_ID and not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("你没有权限操作这个命令捏！", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        if interaction.channel_id in EXPLORATION_TARGET_CHANNEL_IDS:
            # 调用强制重发逻辑
            await self.refresh_channel_daily_panel(interaction.channel, resend=True)
            await interaction.followup.send("日报面板已强制重发最新版！", ephemeral=True)
        else:
            await interaction.followup.send("此命令只能在已配置的日报频道中使用。", ephemeral=True)


    # --- 以下命令保持不变 ---
    @app_commands.command(name="更新搜索面板", description="[管理] 清理旧面板并发送新的搜索面板")
    @app_commands.default_permissions(administrator=True)
    async def refresh_search_panel(self, interaction: discord.Interaction):
        if interaction.user.id != ADMIN_USER_ID and not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("你没有权限操作这个命令捏！", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        try:
            async for msg in interaction.channel.history(limit=50):
                if msg.author == self.bot.user and msg.embeds and msg.embeds[0].title == "🔍 奇米蛋搜索雷达":
                    await msg.delete(); await asyncio.sleep(0.5)
        except Exception as e: print(f"清理搜索面板失败: {e}")

        embed = discord.Embed(
            title="🔍 奇米蛋搜索雷达",
            description="欢迎使用全服务器帖子搜索功能来捉！\n点击下方按钮开始吧！",
            color=0x87ceeb
        )
        embed.set_footer(text="此面板永久有效")
        await interaction.channel.send(embed=embed, view=SearchMethodView())
        await interaction.followup.send("最新的搜索雷达已部署！", ephemeral=True)

    @app_commands.command(name="快捷搜索", description="调出快捷搜索面板")
    async def search_cmd(self, interaction: discord.Interaction):
        await interaction.response.send_message(view=SearchMethodView(), ephemeral=True)