# cogs/statistics/views.py

import discord
from discord import ui
from discord.ext import commands
from datetime import datetime

from . import utils
from . import db
from config import TZ_SHANGHAI

# =================================================================
#  View 1: Forum Selection (频道选择视图)
# =================================================================

class ForumSelectView(ui.View):
    """一个包含论坛选择器和确认按钮的视图"""
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=180)
        self.bot = bot
        self.channel_select = ui.ChannelSelect(
            placeholder="请选择一个论坛频道...",
            channel_types=[discord.ChannelType.forum],
            min_values=1,
            max_values=1
        )
        self.add_item(self.channel_select)

    @ui.button(label="生成报告", style=discord.ButtonStyle.success)
    async def confirm_button(self, interaction: discord.Interaction, button: ui.Button):
        if not self.channel_select.values:
            return await interaction.response.send_message("你还没有选择任何频道！", ephemeral=True)

        selected_channel_stub = self.channel_select.values[0]

        # --- 【核心修复】获取完整的频道对象 ---
        target_forum = interaction.guild.get_channel(selected_channel_stub.id)
        if not target_forum:
            try:
                target_forum = await interaction.guild.fetch_channel(selected_channel_stub.id)
            except (discord.NotFound, discord.Forbidden):
                return await interaction.response.send_message("❌ 无法访问您选择的频道，请检查权限或频道是否存在。", ephemeral=True)

        if not isinstance(target_forum, discord.ForumChannel):
             return await interaction.response.send_message("❌ 您选择的不是一个有效的论坛频道。", ephemeral=True)

        await interaction.response.defer(ephemeral=True, thinking=True)

        # 实例化新的ContainerView
        view = StatisticsContainerView(self.bot, target_forum.id, interaction.guild.id)

        # 传入 interaction 以便正确获取 guild 和 channel 对象
        await view.refresh_data_and_update(interaction=interaction)

        try:
            # 在频道里发送公开的面板
            msg = await interaction.channel.send(view=view)
            # 在数据库记录这个面板
            await db.add_panel_record(msg.id, msg.channel.id, msg.guild.id, target_forum.id)
            # 通知用户操作成功
            await interaction.followup.send("✅ 统计面板已成功生成！", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ 生成面板时出错: {e}", ephemeral=True)


# =================================================================
#  View 2: Statistics Display (统计数据显示视图)
# =================================================================

class StatisticsContainerView(ui.LayoutView):
    def __init__(self, bot: commands.Bot, forum_id: int, guild_id: int):
        super().__init__(timeout=None) # 持久化视图
        self.bot = bot
        self.forum_id = forum_id
        self.guild_id = guild_id
        # 添加一个初始占位符，防止视图为空
        self.add_item(ui.Button(label="正在加载数据...", disabled=True))

    def create_item_section(self, thread_data: dict, is_hot: bool) -> ui.Section:
        """根据帖子数据创建一个 Section 组件"""
        thread = thread_data['thread']
        reaction_count = thread_data['reaction_count']

        stats_description = f"❤️ {reaction_count} 点赞"

        button = ui.Button(label="直达", url=thread.jump_url, style=discord.ButtonStyle.link)

        return ui.Section(
            ui.TextDisplay(content=f"**{thread.name}**"),
            ui.TextDisplay(content=stats_description),
            accessory=button
        )

    async def refresh_data_and_update(self, interaction: discord.Interaction = None):
        """获取最新数据，并重建整个Container视图"""
        # 1. 获取 Guild 对象
        target_guild = self.bot.get_guild(self.guild_id)
        if not target_guild and interaction:
            target_guild = interaction.guild
        if not target_guild: # 如果还是找不到就放弃
             self.clear_items(); self.add_item(ui.Button(label="错误: 找不到服务器", style=discord.ButtonStyle.danger)); return

        # 2. 获取 ForumChannel 对象
        forum = target_guild.get_channel(self.forum_id)
        if not forum:
            try: forum = await target_guild.fetch_channel(self.forum_id)
            except discord.NotFound: self.clear_items(); self.add_item(ui.Button(label="错误: 找不到论坛频道", style=discord.ButtonStyle.danger)); return

        # 3. 获取统计数据
        stats = await utils.fetch_forum_stats(forum)

        self.clear_items()

        # 4. 构建组件列表
        hot_sections = [self.create_item_section(p, is_hot=True) for p in stats['hottest_posts']]
        cold_sections = [self.create_item_section(p, is_hot=False) for p in stats['coldest_posts']]

        update_time_str = datetime.now(TZ_SHANGHAI).strftime('%Y年%m月%d日 %H:%M')

        refresh_btn = ui.Button(label="手动刷新", style=discord.ButtonStyle.primary, emoji="🔄")
        async def refresh_callback(inter: discord.Interaction):
            await inter.response.defer()
            await self.refresh_data_and_update(interaction=inter)
            await inter.edit_original_response(view=self)
        refresh_btn.callback = refresh_callback

        components_to_add = [
            ui.Section(
                ui.TextDisplay(content=f"## 📊 频道统计 · {forum.name}"),
                ui.TextDisplay(content="深入洞察频道的活跃趋势与内容价值。"),
                accessory=ui.Thumbnail(media=target_guild.icon.url if target_guild.icon else None)
            ),
            ui.Section(
                ui.TextDisplay(content=f"**帖子总数**: {stats['total_threads']}"),
                ui.TextDisplay(content=f"**近7日新增**: {stats['recent_threads_count']}"),
                accessory=ui.Button(label="数据概览", style=discord.ButtonStyle.secondary, disabled=True)
            ),
            ui.Separator(),
        ]

        if hot_sections:
            components_to_add.append(ui.TextDisplay(content="### 🔥 近期热门"))
            components_to_add.extend(hot_sections)

        if cold_sections:
            components_to_add.append(ui.Separator())
            components_to_add.append(ui.TextDisplay(content="### 🧊 冷门遗珠"))
            components_to_add.extend(cold_sections)

        components_to_add.extend([
            ui.Separator(),
            ui.Section(ui.TextDisplay(content=f"数据更新于: {update_time_str}")),
            ui.ActionRow(refresh_btn),
        ])

        # 5. 创建并添加 Container
        # 打印组件总数以进行最终调试
        print(f"[StatisticsContainer] Assembling Container with {len(components_to_add)} components.")

        container = ui.Container(
            *components_to_add,
            accent_colour=discord.Color.from_rgb(139, 195, 74)
        )
        self.add_item(container)
