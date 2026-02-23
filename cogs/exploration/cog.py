# cogs/exploration/cog.py

import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime
import asyncio

from config import EXPLORATION_TARGET_CHANNEL_IDS, ADMIN_USER_ID, TZ_SHANGHAI
from .views import SearchPanelContainer, SearchResultContainer
from ..core.db import get_panel_message_id, set_panel_message_id, remove_panel_record

class ExplorationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.add_view(SearchPanelContainer(self.bot))
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
        view = SearchResultContainer(threads, title=panel_title, user=self.bot.user, is_daily=True)

        message_id = await get_panel_message_id(channel.id, 'daily_report')
        target_msg = None

        if message_id and not resend:
            try:
                target_msg = await channel.fetch_message(message_id)
                await target_msg.edit(view=view)
                return
            except discord.NotFound:
                await remove_panel_record(channel.id, 'daily_report')
            except Exception as e:
                print(f"编辑数据库记录的日报面板({message_id})失败: {e}")

        if resend and message_id:
            try:
                old_msg = await channel.fetch_message(message_id)
                await old_msg.delete()
            except discord.NotFound:
                pass
            finally:
                await remove_panel_record(channel.id, 'daily_report')

        if resend:
            try:
                async for msg in channel.history(limit=20):
                     if msg.author == self.bot.user and msg.components:
                         is_daily_report = False
                         for comp in msg.components[0].children:
                             if isinstance(comp, discord.ui.Container):
                                 if comp.accent_colour == discord.Color.gold():
                                     is_daily_report = True
                                     break
                         if is_daily_report:
                            await msg.delete()
            except Exception: pass
        new_msg = await channel.send(view=view)
        await set_panel_message_id(channel.id, new_msg.id, 'daily_report')


    @tasks.loop(minutes=10)
    async def daily_task(self):
        """自动任务：调用数据库驱动的刷新逻辑"""
        for channel_id in EXPLORATION_TARGET_CHANNEL_IDS:
            if channel := self.bot.get_channel(channel_id):
                await self.refresh_channel_daily_panel(channel, resend=False)

    @daily_task.before_loop
    async def before_daily_task(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="更新日报面板", description="[管理] 强制刷新并重发本频道的日报面板")
    @app_commands.default_permissions(administrator=True)
    async def manual_daily_report(self, interaction: discord.Interaction):
        if interaction.user.id != ADMIN_USER_ID and not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("你没有权限操作这个命令捏！", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        if interaction.channel_id in EXPLORATION_TARGET_CHANNEL_IDS:
            await self.refresh_channel_daily_panel(interaction.channel, resend=True)
            await interaction.followup.send("日报面板已强制重发最新版！", ephemeral=True)
        else:
            await interaction.followup.send("此命令只能在已配置的日报频道中使用。", ephemeral=True)

    @app_commands.command(name="更新搜索面板", description="[管理] 清理旧面板并发送新的搜索面板")
    @app_commands.default_permissions(administrator=True)
    async def refresh_search_panel(self, interaction: discord.Interaction):
        if interaction.user.id != ADMIN_USER_ID and not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("你没有权限操作这个命令捏！", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        try:
            async for msg in interaction.channel.history(limit=50):
                if msg.author == self.bot.user and msg.components:
                    is_search_panel = False
                    for comp in msg.components[0].children:
                        if isinstance(comp, discord.ui.Container):
                             for item in comp.children:
                                 if isinstance(item, discord.ui.ActionRow):
                                     for btn in item.children:
                                         if getattr(btn, 'custom_id', None) == 'search_panel_btn_keyword_v2':
                                             is_search_panel = True
                                             break
                                 if is_search_panel: break
                        if is_search_panel: break
                    if is_search_panel:
                        await msg.delete(); await asyncio.sleep(0.5)
        except Exception as e: print(f"清理搜索面板失败: {e}")
        await interaction.channel.send(view=SearchPanelContainer(self.bot))
        await interaction.followup.send("最新的搜索雷达已部署！", ephemeral=True)

    @app_commands.command(name="快捷搜索", description="调出快捷搜索面板")
    async def search_cmd(self, interaction: discord.Interaction):
        await interaction.response.send_message(view=SearchPanelContainer(self.bot), ephemeral=True)