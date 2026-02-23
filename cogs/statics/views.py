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

        target_forum = self.channel_select.values[0]

        await interaction.response.defer()

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

        # === 修改：更新 Container 显示内容 ===
        container = ui.Container(
            # 1. 标题和总体数据
            ui.Section(
                ui.TextDisplay(content=f"### 📊 `{forum.name}` 数据报告"),
                # 显示总帖子数和7日新增
                ui.TextDisplay(content=f"**总帖子数:** {stats['total_posts']} 篇"),
                ui.TextDisplay(content=f"**近7日新增:** {stats['recent_posts_count']} 篇"),
                accessory=ui.Thumbnail(media=forum.guild.icon.url if forum.guild.icon else None)
            ),
            ui.Separator(spacing=discord.SeparatorSpacing.large),

            # 2. 最热帖子
            ui.TextDisplay(content="#### 🔥 本周最热帖子 Top 5"),
            ui.TextDisplay(content=hottest_list_str),

            ui.Separator(),

            # 3. 最冷清帖子
            ui.TextDisplay(content="#### 🧊 本周冷门宝藏 Top 5"),
            ui.TextDisplay(content=coldest_list_str),

            accent_colour=discord.Color.from_rgb(114, 137, 218)
        )

        self.add_item(container)
