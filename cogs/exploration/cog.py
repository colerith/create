# cogs/exploration/cog.py

import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime
import asyncio

from config import EXPLORATION_TARGET_CHANNEL_IDS, ADMIN_USER_ID, TZ_SHANGHAI
from .views import SearchPanelContainer, SearchResultContainer

class ExplorationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # 注册持久化 View
        self.bot.add_view(SearchPanelContainer(self.bot)) 
        self.daily_task.start()

    async def cog_unload(self):
        self.daily_task.cancel()

    async def get_todays_threads(self, guild: discord.Guild) -> list[discord.Thread]:
        """获取今天发布的所有帖子"""
        today_start_ts = datetime.now(TZ_SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

        threads_list = []
        for forum in guild.forums:
            if not forum.permissions_for(guild.me).read_messages:
                continue
            threads_list.extend([t for t in forum.threads if t.created_at.timestamp() >= today_start_ts])

        threads_list.sort(key=lambda t: t.created_at, reverse=True)
        return threads_list

    async def refresh_channel_daily_report(self, channel: discord.TextChannel):
        """
        刷新每日更新日报 (Container Ver.) - 智能编辑模式
        """
        threads = await self.get_todays_threads(channel.guild)
        date_str = datetime.now(TZ_SHANGHAI).strftime('%Y年%m月%d日')
        panel_title = f"📅 {date_str} 更新日报"

        # 创建 Container View
        view = SearchResultContainer(threads, title=panel_title, user=self.bot.user, is_daily=True)

        # 查找已有面板
        target_msg = None
        try:
            async for msg in channel.history(limit=20):
                if msg.author == self.bot.user:
                    if msg.components:
                        target_msg = msg
                        break
        except Exception as e:
            print(f"扫描日报面板历史出错: {e}")

        if target_msg:
            try:
                # 编辑模式
                await target_msg.edit(view=view)
                return
            except discord.NotFound:
                pass
            except Exception as e:
                print(f"编辑日报面板失败: {e}")

        # 发送新面板
        if target_msg:
            try: await target_msg.delete()
            except: pass
        await channel.send(view=view)

    @tasks.loop(minutes=10)
    async def daily_task(self):
        for channel_id in EXPLORATION_TARGET_CHANNEL_IDS:
            if channel := self.bot.get_channel(channel_id):
                await self.refresh_channel_daily_report(channel)

    @daily_task.before_loop
    async def before_daily_task(self):
        await self.bot.wait_until_ready()

    # --- 新增/修改后的命令 ---

    @app_commands.command(name="更新日报面板", description="[管理] 刷新本频道的日报内容")
    @app_commands.default_permissions(administrator=True)
    async def manual_daily_report(self, interaction: discord.Interaction):
        if interaction.user.id != ADMIN_USER_ID and not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("你没有权限操作这个命令捏！", ephemeral=True)

        if interaction.channel_id not in EXPLORATION_TARGET_CHANNEL_IDS:
            await interaction.response.send_message("⚠️ 注意：当前频道不在自动更新列表内。", ephemeral=True)
        else:
            await interaction.response.defer(ephemeral=True)

        await self.refresh_channel_daily_report(interaction.channel)

        if not interaction.response.is_done():
             await interaction.response.send_message("✅ 日报面板已刷新！", ephemeral=True)
        else:
             await interaction.followup.send("✅ 日报面板已刷新！", ephemeral=True)

    @app_commands.command(name="更新搜索面板", description="[管理] 刷新本频道的搜索雷达视图")
    @app_commands.default_permissions(administrator=True)
    async def refresh_search_panel(self, interaction: discord.Interaction):
        if interaction.user.id != ADMIN_USER_ID and not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("你没有权限操作这个命令捏！", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        # 定义新视图 (传入 self.bot)
        new_view = SearchPanelContainer(self.bot)

        # 尝试查找已有面板并编辑
        target_msg = None

        if target_msg:
            try:
                await target_msg.edit(view=new_view)
                await interaction.followup.send("✅ 搜索面板已更新（原位编辑）！", ephemeral=True)
                return
            except discord.NotFound:
                pass
            except Exception:
                pass

        # 如果没找到或编辑失败，发送新的
        await interaction.channel.send(view=new_view)
        await interaction.followup.send("✅ 最新的搜索雷达已重新发送！", ephemeral=True)

    @app_commands.command(name="快捷搜索", description="调出临时搜索面板")
    async def search_cmd(self, interaction: discord.Interaction):
        # 瞬态面板 (传入 self.bot)
        await interaction.response.send_message(
            view=SearchPanelContainer(self.bot), 
            ephemeral=True
        )