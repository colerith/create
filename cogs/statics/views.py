# cogs/statistics/views.py

import discord
from discord import ui

from . import utils

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

        # 如果 get_channel 失败 (例如缓存问题)，尝试 fetch_channel
        if not target_forum:
            try:
                target_forum = await interaction.guild.fetch_channel(app_command_channel.id)
            except (discord.InvalidData, discord.HTTPException, discord.NotFound, discord.Forbidden):
                 return await interaction.response.send_message("❌ 无法获取有效的频道信息，请重试。", ephemeral=True)


        # 确保获取到的是 ForumChannel
        if not isinstance(target_forum, discord.ForumChannel):
             return await interaction.response.send_message("❌ 您选择的不是一个有效的论坛频道。", ephemeral=True)


        await interaction.response.defer()

        # 现在传入的是完整的 ForumChannel 对象
        stats = await utils.fetch_forum_stats(target_forum)

        view = StatisticsContainerView(target_forum, stats)

        from . import db
        msg = await interaction.followup.send(view=view)
        await db.add_panel_record(msg.id, msg.channel.id, msg.guild.id, target_forum.id)


class StatisticsContainerView(ui.LayoutView):
    """使用 Container 展示统计数据"""
    def __init__(self, forum: discord.ForumChannel, stats: dict):
        super().__init__(timeout=None)

        def format_post_list(post_data):
            if not post_data:
                return "- 暂无数据"

            lines = []
            for item in post_data:
                thread = item["thread"]
                likes = item.get('likes', 0)
                comments = item.get('comments', 0)
                lines.append(f"- [{thread.name[:30]}]({thread.jump_url}) - **{likes}👍 / {comments}💬**")
            return "\n".join(lines)

        hottest_list_str = format_post_list(stats['hottest_posts'])
        coldest_list_str = format_post_list(stats['coldest_posts'])

        container = ui.Container(
            ui.Section(
                ui.TextDisplay(content=f"### 📊 `{forum.name}` 数据报告"),
                ui.TextDisplay(content=f"**总帖子数:** {stats['total_posts']} 篇"),
                ui.TextDisplay(content=f"**近7日新增:** {stats['recent_posts_count']} 篇"),
                accessory=ui.Thumbnail(media=forum.guild.icon.url if forum.guild.icon else None)
            ),
            ui.Separator(spacing=discord.SeparatorSpacing.large),
            ui.TextDisplay(content="#### 🔥 本周最热帖子 Top 5"),
            ui.TextDisplay(content=hottest_list_str),
            ui.Separator(),
            ui.TextDisplay(content="#### 🧊 本周冷门宝藏 Top 5"),
            ui.TextDisplay(content=coldest_list_str),
            accent_colour=discord.Color.from_rgb(114, 137, 218)
        )

        self.add_item(container)