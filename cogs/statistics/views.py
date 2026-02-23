import discord
from discord import ui
from datetime import datetime
from . import db as statistics_db
import asyncio

class StatisticsContainerView(ui.LayoutView):
    def __init__(self, stats_data: dict):
        super().__init__(timeout=None)
        self.clear_items()

        elements = []
        MAX_HOT_THREADS = 6  # 最多显示的热门帖子数
        MAX_COLD_THREADS = 5  # 最多显示的冷门帖子数

        # --- 1. 顶部主标题 ---
        elements.append(
            ui.Section(
                ui.TextDisplay(content=f"### 📊 频道统计 · {stats_data.get('channel_name', '加载中')}"),
                ui.TextDisplay(content="深入洞察频道的活跃趋势与内容价值。"),
                accessory=ui.Thumbnail(media=stats_data.get('channel_icon_url', "https://upload.wikimedia.org/wikipedia/commons/c/ca/1x1.png"))
            )
        )

        # --- 2. 数据总览 ---
        elements.append(
            ui.Section(
                ui.TextDisplay(content=f"**总帖子数:** {stats_data.get('total_threads', 0)}"),
                ui.TextDisplay(content=f"**近7日新增:** {stats_data.get('new_threads_7d', 0)}"),
                accessory=ui.Button(label="数据概览", style=discord.ButtonStyle.success, disabled=True, custom_id="stats_overview_placeholder")
            )
        )

        # --- 3. 热门帖子列表 ---
        elements.append(ui.Separator())
        elements.append(ui.TextDisplay(content="#### 近期热门"))

        hot_threads = stats_data.get('hot_threads', [])
        if hot_threads:
            # 【修复】限制显示数量，防止超限
            for th in hot_threads[:MAX_HOT_THREADS]:
                elements.append(
                    ui.Section(
                        # 【修复】合并两个 TextDisplay 为一个，减少组件数
                        ui.TextDisplay(content=f"**{th.get('name', '无标题')[:70]}**\n-# 👍 {th.get('likes', 0)}  ·  💬 {th.get('comments', 0)}"),
                        accessory=ui.Button(label="直达", style=discord.ButtonStyle.secondary, url=th.get('url'))
                    )
                )
        else:
            elements.append(ui.TextDisplay(content="-# 暂无热门帖子..."))

        # --- 4. 冷门宝藏列表 ---
        elements.append(ui.Separator())
        elements.append(ui.TextDisplay(content="#### 💎 冷门遗珠"))

        cold_threads = stats_data.get('cold_threads', [])
        if cold_threads:
            # 【修复】限制显示数量，防止超限
            for th in cold_threads[:MAX_COLD_THREADS]:
                relative_time = "未知"
                if th.get('created_at'):
                    relative_time = discord.utils.format_dt(th['created_at'], style='R')

                elements.append(
                    ui.Section(
                        # 【修复】合并两个 TextDisplay 为一个，减少组件数
                        ui.TextDisplay(content=f"**{th.get('name', '无标题')[:70]}**\n-# 发布于 {relative_time}"),
                        accessory=ui.Button(label="考古", style=discord.ButtonStyle.secondary, url=th.get('url'))
                    )
                )
        else:
            elements.append(ui.TextDisplay(content="-# 暂无冷门帖子..."))

        # --- 5. 底部刷新按钮 ---
        elements.append(ui.Separator(spacing=discord.SeparatorSpacing.large))

        self.btn_refresh_info = ui.Button(
            label=f"数据更新于 {datetime.now().strftime('%H:%M')}",
            style=discord.ButtonStyle.secondary,
            custom_id="stats_manual_refresh_btn",
            emoji="🔄"
        )
        self.btn_refresh_info.callback = self.on_refresh_button_click
        elements.append(ui.ActionRow(self.btn_refresh_info))

        # --- 组合并构建容器 ---
        container = ui.Container(
            *elements,
            accent_colour=discord.Color.from_rgb(113, 135, 212)
        )
        self.add_item(container)

    async def on_refresh_button_click(self, interaction: discord.Interaction):
        """点击刷新按钮时的回调"""
        await interaction.response.send_message(
            "📋 **关于数据刷新**\n"
            "此面板的数据由机器人每日凌晨自动更新。\n"
            "此按钮仅用于展示最近一次的更新时间，再次点击不会触发即时刷新哦。",
            ephemeral=True
        )


class ForumSelectView(ui.View):
    def __init__(self, cog_instance):
        super().__init__(timeout=180)
        self.cog = cog_instance
        self.channel_select = ui.ChannelSelect(
            placeholder="请选择1-5个论坛频道...",
            channel_types=[discord.ChannelType.forum],
            min_values=1,
            max_values=5,
            custom_id="stats_forum_selector"
        )
        self.add_item(self.channel_select)

    @ui.button(label="生成统计面板", style=discord.ButtonStyle.success, custom_id="stats_confirm_generation_btn")
    async def confirm_button(self, interaction: discord.Interaction, button: ui.Button):
        if not self.channel_select.values:
            return await interaction.response.send_message("你还没有选择任何频道！", ephemeral=True)

        selected_channels = self.channel_select.values

        button.disabled = True
        self.channel_select.disabled = True
        await interaction.response.edit_message(content="收到指令，正在处理，请稍候...", view=self)

        await interaction.followup.send(f"⏳ 开始为 **{len(selected_channels)}** 个频道生成统计面板...", ephemeral=True)

        for channel_stub in selected_channels:
            channel = interaction.guild.get_channel(channel_stub.id)
            if not isinstance(channel, discord.ForumChannel):
                continue

            stats_data = await self.cog.gather_statistics(channel)
            if stats_data is None:
                await interaction.followup.send(f"❌ 无法访问频道 {channel.mention} 或读取其内容。", ephemeral=True)
                continue

            view = StatisticsContainerView(stats_data=stats_data)
            msg = await interaction.channel.send(view=view)
            await statistics_db.add_statistics_panel(msg.id, msg.channel.id, channel.id, interaction.guild.id)
            await asyncio.sleep(1)

        await interaction.edit_original_response(content="✅ 操作完成。", view=None)
        self.stop()