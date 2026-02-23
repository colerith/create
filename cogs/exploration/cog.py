# cogs/exploration/cog.py

import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime
import asyncio

# 确保导入正确的视图和配置
from config import EXPLORATION_TARGET_CHANNEL_IDS, ADMIN_USER_ID, TZ_SHANGHAI
from .views import SearchPanelContainer, SearchResultContainer

class ExplorationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # 注册持久化 View
        # 确保 SearchPanelContainer 的 custom_id 是固定的
        self.bot.add_view(SearchPanelContainer(self.bot))
        self.daily_task.start()

    async def cog_unload(self):
        self.daily_task.cancel()

    async def get_todays_threads(self, guild: discord.Guild) -> list[discord.Thread]:
        """获取今天发布的所有帖子"""
        today_start_ts = datetime.now(TZ_SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

        threads_list = []
        for forum in guild.forums:
            # 机器人需要有读取消息的权限
            if not forum.permissions_for(guild.me).read_messages:
                continue
            threads_list.extend([t for t in forum.threads if t.created_at.timestamp() >= today_start_ts])

        threads_list.sort(key=lambda t: t.created_at, reverse=True)
        return threads_list

    async def refresh_channel_daily_report(self, channel: discord.TextChannel, force_resend: bool = False):
        """
        刷新每日更新日报 (Container Ver.)
        - 默认模式：智能编辑，找到旧面板就更新。
        - 强制重发模式 (force_resend=True)：删除旧的，发送新的。
        """
        threads = await self.get_todays_threads(channel.guild)
        date_str = datetime.now(TZ_SHANGHAI).strftime('%Y年%m月%d日')
        panel_title = f"📅 {date_str} 更新日报"

        # 创建新的 Container View
        view = SearchResultContainer(threads, title=panel_title, user=self.bot.user, is_daily=True)

        target_msg = None
        # 如果不是强制重发，才去查找旧消息
        if not force_resend:
            try:
                # 寻找 Bot 在该频道发的最后一条 component 消息（大概率就是面板）
                async for msg in channel.history(limit=20):
                    if msg.author.id == self.bot.user.id and msg.components:
                        # 进一步确定是日报面板（检查 custom_id 或其他特征）
                        # 简单起见，这里假设最近的一条就是
                        target_msg = msg
                        break
            except Exception as e:
                print(f"扫描日报面板历史出错: {e}")

        # 如果找到了旧消息且不是强制重发，就编辑
        if target_msg and not force_resend:
            try:
                await target_msg.edit(view=view)
                return # 编辑成功，任务完成
            except discord.NotFound:
                pass # 消息被删了，继续向下执行发送逻辑
            except Exception as e:
                print(f"编辑日报面板失败: {e}") # 其他错误，继续向下执行发送逻辑

        # --- 发送新面板的逻辑 ---
        # 适用于：1. 找不到旧面板 2. 编辑失败 3. 强制重发

        # 如果是强制重发，先清理一下
        if force_resend:
            try:
                # 寻找并删除所有旧的日报面板
                async for msg in channel.history(limit=50):
                     if msg.author.id == self.bot.user.id and msg.components:
                         # 理论上应该检查 custom_id 来确保是日报面板
                         await msg.delete()
                         await asyncio.sleep(0.5)
            except Exception as e:
                print(f"清理旧日报面板时出错: {e}")

        # 发送新的
        await channel.send(view=view)


    @tasks.loop(minutes=10)
    async def daily_task(self):
        """自动任务：保持原有的编辑更新逻辑"""
        for channel_id in EXPLORATION_TARGET_CHANNEL_IDS:
            if channel := self.bot.get_channel(channel_id):
                # 调用时 force_resend=False (默认)
                await self.refresh_channel_daily_report(channel)

    @daily_task.before_loop
    async def before_daily_task(self):
        await self.bot.wait_until_ready()

    # --- 新增/修改后的命令 ---

    @app_commands.command(name="更新日报面板", description="[管理] 清理旧面板并发送最新的日报")
    @app_commands.default_permissions(administrator=True)
    async def manual_daily_report(self, interaction: discord.Interaction):
        """手动命令：总是清理并发送新的面板"""
        if interaction.user.id != ADMIN_USER_ID and not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("你没有权限操作这个命令捏！", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        # 调用时强制重发 force_resend=True
        await self.refresh_channel_daily_report(interaction.channel, force_resend=True)

        await interaction.followup.send("✅ 最新的日报面板已发送！", ephemeral=True)


    @app_commands.command(name="更新搜索面板", description="[管理] 刷新本频道的搜索雷达视图")
    @app_commands.default_permissions(administrator=True)
    async def refresh_search_panel(self, interaction: discord.Interaction):
        if interaction.user.id != ADMIN_USER_ID and not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("你没有权限操作这个命令捏！", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        # 定义新视图 (传入 self.bot)
        new_view = SearchPanelContainer(self.bot)

        # 清理旧面板
        try:
            async for msg in interaction.channel.history(limit=50):
                if msg.author.id == self.bot.user.id and msg.components:
                    # 假设最近的带组件消息是搜索面板
                    await msg.delete()
        except:
            pass

        # 发送新的
        await interaction.channel.send(view=new_view)
        await interaction.followup.send("✅ 最新的搜索雷达已重新发送！", ephemeral=True)


    @app_commands.command(name="快捷搜索", description="调出临时搜索面板")
    async def search_cmd(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            view=SearchPanelContainer(self.bot),
            ephemeral=True
        )
