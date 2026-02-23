# cogs/statistics/cog.py

import discord
from discord import app_commands
from discord.ext import commands

from . import db
from .views import ForumSelectView, StatisticsContainerView

class StatisticsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # 数据库初始化
        self.bot.loop.create_task(db.init_statistics_db())
        # 在Bot准备好之后，恢复持久化视图
        self.bot.loop.create_task(self.register_persistent_views_after_ready())

    async def cog_unload(self):
        # 在cog卸载时，理論上需要处理持久化视图相关的逻辑，此处简化
        pass

    async def register_persistent_views_after_ready(self):
        """
        Bot 启动并准备就绪后，从数据库加载并重新注册所有持久化的统计面板。
        """
        await self.bot.wait_until_ready()
        print("🔄 [StatisticsCog] Bot 已就绪，开始恢复统计面板...")
        panel_records = await db.get_all_panel_records()
        if not panel_records:
            print("ℹ️ [StatisticsCog] 数据库中无统计面板记录，无需恢复。")
            return

        count = 0
        for record in panel_records:
            guild_id = record['guild_id']
            forum_id = record['target_forum_id']

            # 【核心修复】创建实例时，传入 guild_id
            view_instance = StatisticsContainerView(self.bot, forum_id, guild_id)

            # 使用 self.bot.add_view() 重新注册，并绑定到原始消息 ID 上
            # 这是 discord.py 2.0+ 的标准持久化视图恢复方法
            self.bot.add_view(view_instance, message_id=record['message_id'])
            count += 1

        print(f"✅ [StatisticsCog] 成功恢复并注册了 {count} 个持久化统计面板。")


    @app_commands.command(name="生成统计面板", description="为指定的论坛频道生成一个动态统计面板。")
    @app_commands.default_permissions(manage_guild=True)
    async def generate_statistics_panel(self, interaction: discord.Interaction):
        """
        命令的入口，发送一个临时的、仅用户可见的视图，用于选择目标论坛频道。
        """
        # 这个命令的逻辑保持不变
        view = ForumSelectView(self.bot)
        await interaction.response.send_message(
            "请在下方选择你想要生成统计报告的论坛频道，然后点击按钮确认。",
            view=view,
            ephemeral=True
        )