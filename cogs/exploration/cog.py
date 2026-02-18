# cogs/exploration/cog.py

import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime
import asyncio

from config import EXPLORATION_TARGET_CHANNEL_IDS, ADMIN_USER_ID, TZ_SHANGHAI
from .views import SearchMethodView, PaginatorView

class ExplorationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.add_view(SearchMethodView())
        self.daily_task.start()

    async def cog_unload(self):
        self.daily_task.cancel()

    async def get_todays_threads(self, guild: discord.Guild) -> list[discord.Thread]:
        """获取今天发布的所有帖子"""
        today_start_ts = datetime.now(TZ_SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        threads_list = []
        for forum in guild.forums:
            if not forum.permissions_for(guild.me).read_messages: continue
            threads_list.extend([t for t in forum.threads if t.created_at.timestamp() >= today_start_ts])
        threads_list.sort(key=lambda t: t.created_at, reverse=True)
        return threads_list

    async def refresh_channel_daily_panel(self, channel: discord.TextChannel, resend: bool = False):
        """刷新每日更新面板"""
        threads = await self.get_todays_threads(channel.guild)
        date_str = datetime.now(TZ_SHANGHAI).strftime('%Y年%m月%d日')
        panel_title = f"📅 {date_str} 更新日报"
        view = PaginatorView(threads, title=panel_title, is_daily=True)
        embed = view.get_embed()

        target_msg = None
        if not resend:
            try:
                async for msg in channel.history(limit=20):
                    if msg.author == self.bot.user and msg.embeds and "更新日报" in msg.embeds[0].title:
                        target_msg = msg
                        break
            except Exception as e: print(f"扫描日报面板历史出错: {e}")

        if target_msg:
            try: await target_msg.edit(embed=embed, view=view)
            except discord.NotFound: await channel.send(embed=embed, view=view)
        else:
            if resend:
                try:
                    async for msg in channel.history(limit=50):
                         if msg.author == self.bot.user and msg.embeds and "更新日报" in msg.embeds[0].title: await msg.delete()
                except: pass
            await channel.send(embed=embed, view=view)

    @tasks.loop(minutes=10)
    async def daily_task(self):
        for channel_id in EXPLORATION_TARGET_CHANNEL_IDS:
            if channel := self.bot.get_channel(channel_id):
                await self.refresh_channel_daily_panel(channel)

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
            await interaction.followup.send("日报面板已清理并发送最新版惹！", ephemeral=True)
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