import discord
from discord import ui
from datetime import datetime
from . import db as statistics_db
import asyncio

class StatisticsContainerView(ui.LayoutView):
    def __init__(self, stats_data: dict):
        super().__init__(timeout=None)

        self.btn_refresh_info = ui.Button(
            label=f"数据更新于 {datetime.now().strftime('%H:%M:%S')}",
            style=discord.ButtonStyle.secondary,
            disabled=True,
            custom_id="stats_panel_info_btn"
        )
        self.clear_items()

        header_section = ui.Section(
            ui.TextDisplay(content=f"### 📊 频道统计: {stats_data['channel_name']}"),
            ui.TextDisplay(content=f"**总帖子数:** {stats_data['total_threads']} | **近7日新增:** {stats_data['new_threads_7d']}"),
            accessory=ui.Thumbnail(media=stats_data.get('channel_icon_url', "https://cdn.discordapp.com/embed/avatars/0.png"))
        )

        elements = [header_section]

        hot_threads = stats_data.get('hot_threads', [])
        if hot_threads:
            # Markdown链接现在直接在 TextDisplay 中受支持
            hot_content = "\n".join([f"[`👍{th['likes']} | 💬{th['comments']}`] [{th['name']}]({th['url']})" for th in hot_threads])
            elements.extend([ui.Separator(), ui.TextDisplay(content="#### 🔥 热门帖子"), ui.TextDisplay(content=hot_content or "暂无数据")])

        cold_threads = stats_data.get('cold_threads', [])
        if cold_threads:
            cold_content = "\n".join([f"[`👍{th['likes']} | 💬{th['comments']}`] [{th['name']}]({th['url']})" for th in cold_threads])
            elements.extend([ui.Separator(), ui.TextDisplay(content="#### 💎 冷门宝藏"), ui.TextDisplay(content=cold_content or "暂无数据")])

        elements.extend([ui.Separator(spacing=discord.SeparatorSpacing.large), ui.ActionRow(self.btn_refresh_info)])

        container = ui.Container(*elements, accent_colour=discord.Color.from_rgb(88, 101, 242))
        self.add_item(container)


class ForumSelectView(ui.View):
    def __init__(self, cog_instance):
        super().__init__(timeout=180)
        # 将cog实例作为参数传入，以便调用其方法
        self.cog = cog_instance
        self.channel_select = ui.ChannelSelect(
            placeholder="请选择1-5个论坛频道...",
            channel_types=[discord.ChannelType.forum],
            min_values=1,
            max_values=5, # 限制最多选5个
            custom_id="stats_forum_selector"
        )
        self.add_item(self.channel_select)

    @ui.button(label="生成统计面板", style=discord.ButtonStyle.success)
    async def confirm_button(self, interaction: discord.Interaction, button: ui.Button):
        if not self.channel_select.values:
            return await interaction.response.send_message("你还没有选择任何频道！", ephemeral=True)

        selected_channels = self.channel_select.values

        await interaction.response.defer(thinking=True, ephemeral=True)
        # 编辑原始的 ephemeral 消息，移除视图
        await interaction.edit_original_response(content=f"收到指令！正在为 {len(selected_channels)} 个频道生成统计面板，请稍候...", view=None)

        # 循环处理每个选择的频道
        for channel_stub in selected_channels:
            # channel_stub 是一个简化的对象，需要获取完整的频道对象
            channel = interaction.guild.get_channel(channel_stub.id)
            if not isinstance(channel, discord.ForumChannel):
                continue

            stats_data = await self.cog.gather_statistics(channel)
            if stats_data is None:
                await interaction.followup.send(f"❌ 无法访问频道 {channel.mention} 或读取其内容。", ephemeral=True)
                continue

            view = StatisticsContainerView(stats_data=stats_data)

            # 在执行命令的频道发送面板
            msg = await interaction.channel.send(view=view)

            # 记录到数据库以便每日刷新
            await statistics_db.add_statistics_panel(msg.id, msg.channel.id, channel.id, interaction.guild.id)
            await asyncio.sleep(1) # API友好

        await interaction.followup.send("✅ 所有统计面板均已创建！", ephemeral=True)
        self.stop()