# cogs/statistics/views.py

import discord
from discord import ui
from datetime import datetime

from . import utils
from config import TZ_SHANGHAI

class ForumSelectView(ui.View):
    """一个包含论坛选择器和确认按钮的视图"""
    def __init__(self, bot):
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

        app_command_channel = self.channel_select.values[0]
        target_forum = interaction.guild.get_channel(app_command_channel.id)

        if not target_forum:
            try:
                target_forum = await interaction.guild.fetch_channel(app_command_channel.id)
            except:
                 return await interaction.response.send_message("❌ 无法获取有效的频道信息，请重试。", ephemeral=True)

        if not isinstance(target_forum, discord.ForumChannel):
             return await interaction.response.send_message("❌ 您选择的不是一个有效的论坛频道。", ephemeral=True)

        # 【修复】调整交互流程
        await interaction.response.defer() # 1. 立即响应交互，防止超时

        # 2. 创建视图实例
        view = StatisticsContainerView(self.bot, target_forum.id)

        # 3. 让视图实例自己去获取数据并构建组件，但不发送消息
        await view.refresh_data_and_update() # 此方法现在不再处理消息发送

        # 4. 在回调函数中统一发送消息
        msg = await interaction.followup.send(view=view, ephemeral=False)

        # 5. 确保 msg 不是 None 后再存入数据库
        if msg:
            from . import db
            # 注意：followup.send() 返回的是 WebhookMessage, 它有 id, channel, guild 等属性，可以直接用
            await db.add_panel_record(msg.id, msg.channel.id, msg.guild.id, target_forum.id)
        else:
            print("❌ [StatisticsCog] Followup message is None, failed to record panel to DB.")


class StatisticsContainerView(ui.LayoutView):
    """
    使用多个 Section 和 ActionRow 精确模拟截图样式的统计面板
    """
    def __init__(self, bot: discord.Client, forum_id: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.forum_id = forum_id
        # 此处不再添加按钮，而是在 refresh_data_and_update 中动态构建

    def create_refresh_button(self):
        """创建一个固定的手动刷新按钮"""
        btn = ui.Button(
            label="手动刷新",
            style=discord.ButtonStyle.secondary,
            emoji="🔄",
            custom_id=f"stats_refresh_{self.forum_id}"
        )
        btn.callback = self.manual_refresh_callback
        return btn

    async def manual_refresh_callback(self, interaction: discord.Interaction):
        """手动刷新按钮的回调函数"""
        # 按钮的回调不能用 followup，要用 interaction.response.edit_message
        await interaction.response.defer()
        await self.refresh_data_and_update(interaction_to_edit=interaction)

    def create_item_section(self, thread_data: dict, is_hot: bool):
        """为单个帖子创建 Section"""
        thread = thread_data["thread"]
        likes = thread_data.get('likes', 0)
        comments = thread_data.get('comments', 0)

        stats_description = f"💬 {comments} 回复"
        if likes > 0:
             stats_description = f"👍 {likes} 点赞 · {stats_description}"

        button = ui.Button(
            label="直达" if is_hot else "考古",
            style=discord.ButtonStyle.link,
            url=thread.jump_url,
            emoji="🔗"
        )
        return ui.Section(
            ui.TextDisplay(content=f"**{thread.name}**"),
            ui.TextDisplay(content=stats_description),
            accessory=button
        )

    # 【修复】修改方法签名，让其更通用
    async def refresh_data_and_update(self, *, interaction_to_edit: discord.Interaction = None, message_to_edit: discord.Message = None):
        """
        核心函数：获取数据并更新整个 View。
         interaction_to_edit: 从按钮点击触发的交互
         message_to_edit: 从后台任务触发的消息对象
        """
        guild = None
        current_guild_id = None

        # 统一获取 guild 对象和 ID 的方式
        if interaction_to_edit:
             guild = interaction_to_edit.guild
        elif message_to_edit:
            guild = message_to_edit.guild

        if not guild:
            # 如果都找不到，尝试从 bot 的缓存中获取
            try:
                guild = self.bot.get_guild(self.bot.get_channel(self.forum_id).guild.id)
            except:
                print(f"❌ 无法确定服务器来刷新统计面板 (Forum ID: {self.forum_id})")
                return

        forum = guild.get_channel(self.forum_id)
        if not isinstance(forum, discord.ForumChannel): return

        stats = await utils.fetch_forum_stats(forum)

        self.clear_items()

        header_section = ui.Section(
            ui.TextDisplay(content=f"### 📊 频道统计・{forum.name}"),
            ui.TextDisplay(content="深入洞察频道的活跃趋势与内容价值。"),
            accessory=ui.Thumbnail(media=guild.icon.url if guild.icon else "https://cdn.discordapp.com/embed/avatars/0.png")
        )

        overview_section = ui.Section(
            ui.TextDisplay(content=f"**帖子总数**：`{stats['total_posts']}`"),
            ui.TextDisplay(content=f"**近7日新增**：`{stats['recent_posts_count']}`"),
            accessory=ui.Button(label="数据概览", style=discord.ButtonStyle.success, disabled=True)
        )

        hot_sections = [self.create_item_section(p, is_hot=True) for p in stats['hottest_posts']]
        cold_sections = [self.create_item_section(p, is_hot=False) for p in stats['coldest_posts']]

        update_time_str = datetime.now(TZ_SHANGHAI).strftime('%Y年%m月%d日 %H:%M')
        footer_section = ui.Section(
            ui.TextDisplay(content=f"数据更新于：{update_time_str}"),
            accessory=ui.Button(label=" ", style=discord.ButtonStyle.secondary, disabled=True)
        )

        refresh_button_row = ui.ActionRow(self.create_refresh_button())

        container = ui.Container(
            header_section, overview_section,
            ui.Separator(), ui.TextDisplay(content="### 🔥 近期热门"), *hot_sections,
            ui.Separator(), ui.TextDisplay(content="### 🧊 冷门遗珠"), *cold_sections,
            ui.Separator(), footer_section, refresh_button_row,
            accent_colour=discord.Color.blue()
        )
        self.add_item(container)

        # 统一处理消息编辑
        if interaction_to_edit:
            try:
                await interaction_to_edit.edit_original_response(view=self)
            except discord.NotFound:
                print(f"尝试编辑交互消息失败: {interaction_to_edit.message.id} (消息可能已被删除)")
        elif message_to_edit:
            try:
                await message_to_edit.edit(view=self)
            except discord.NotFound:
                print(f"尝试编辑后台消息失败: {message_to_edit.id} (消息可能已被删除)")