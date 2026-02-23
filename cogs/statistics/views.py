import discord
from discord import ui
from datetime import datetime
from . import db as statistics_db
import asyncio

class StatisticsContainerView(ui.LayoutView):
    """
    【已升级为分页视图】
    通过内部状态和按钮切换，将热门和冷门列表分在不同页面展示，
    确保任何时候组件数量都远低于40个的上限。
    """
    def __init__(self, stats_data: dict):
        super().__init__(timeout=None)
        self.stats_data = stats_data
        self.current_page = "hot"  # 默认显示热门帖子页面

        # 首次构建视图
        self.update_view()

    def update_view(self):
        """
        核心函数：根据当前页面状态 (self.current_page) 重新构建整个容器。
        """
        self.clear_items()
        elements = []
        MAX_THREADS_PER_PAGE = 15  # 每页最多显示的帖子数，可以适当调高

        # === 1. 静态顶部内容 (所有页面共用) ===
        elements.append(
            ui.Section(
                ui.TextDisplay(content=f"### 📊 频道统计 · {self.stats_data.get('channel_name', '加载中')}"),
                ui.TextDisplay(content="😋来看看有什么好帖子吧！"),
                accessory=ui.Thumbnail(media=self.stats_data.get('channel_icon_url', "https://upload.wikimedia.org/wikipedia/commons/c/ca/1x1.png"))
            )
        )
        elements.append(
            ui.Section(
                ui.TextDisplay(content=f"**总帖子数:** {self.stats_data.get('total_threads', 0)}"),
                ui.TextDisplay(content=f"**近7日新增:** {self.stats_data.get('new_threads_7d', 0)}"),
                accessory=ui.Button(label="数据概览", style=discord.ButtonStyle.success, disabled=True, custom_id="stats_overview_placeholder")
            )
        )
        elements.append(ui.Separator())

        # === 2. 页面切换按钮 (分页核心) ===
        self.btn_hot = ui.Button(
            label="近期热门",
            style=discord.ButtonStyle.primary,
            custom_id="stats_page_hot",
            disabled=(self.current_page == "hot") # 当前页的按钮禁用
        )
        self.btn_hot.callback = self.on_page_switch

        self.btn_cold = ui.Button(
            label="💎 冷门遗珠",
            style=discord.ButtonStyle.primary,
            custom_id="stats_page_cold",
            disabled=(self.current_page == "cold") # 当前页的按钮禁用
        )
        self.btn_cold.callback = self.on_page_switch
        elements.append(ui.ActionRow(self.btn_hot, self.btn_cold))


        # === 3. 动态内容区 (根据 self.current_page 决定显示什么) ===
        if self.current_page == "hot":
            hot_threads = self.stats_data.get('hot_threads', [])
            if hot_threads:
                for th in hot_threads[:MAX_THREADS_PER_PAGE]:
                    elements.append(
                        ui.Section(
                            ui.TextDisplay(content=f"**{th.get('name', '无标题')[:70]}**\n-# 👍 {th.get('likes', 0)}  ·  💬 {th.get('comments', 0)}"),
                            accessory=ui.Button(label="直达", style=discord.ButtonStyle.secondary, url=th.get('url'))
                        )
                    )
            else:
                elements.append(ui.TextDisplay(content="-# 暂无热门帖子..."))

        elif self.current_page == "cold":
            cold_threads = self.stats_data.get('cold_threads', [])
            if cold_threads:
                for th in cold_threads[:MAX_THREADS_PER_PAGE]:
                    relative_time = discord.utils.format_dt(th['created_at'], style='R') if th.get('created_at') else "未知"
                    elements.append(
                        ui.Section(
                            ui.TextDisplay(content=f"**{th.get('name', '无标题')[:70]}**\n-# 发布于 {relative_time}"),
                            accessory=ui.Button(label="考古", style=discord.ButtonStyle.secondary, url=th.get('url'))
                        )
                    )
            else:
                elements.append(ui.TextDisplay(content="-# 暂无冷门帖子..."))

        # === 4. 静态底部内容 (所有页面共用) ===
        elements.append(ui.Separator(spacing=discord.SeparatorSpacing.large))
        self.btn_refresh_info = ui.Button(
            label=f"数据更新于 {datetime.now().strftime('%H:%M')}",
            style=discord.ButtonStyle.secondary,
            custom_id="stats_manual_refresh_btn",
            emoji="🔄"
        )
        self.btn_refresh_info.callback = self.on_refresh_button_click
        elements.append(ui.ActionRow(self.btn_refresh_info))

        # --- 最终组合并构建容器 ---
        container = ui.Container(
            *elements,
            accent_colour=discord.Color.from_rgb(113, 135, 212)
        )
        self.add_item(container)

    async def on_page_switch(self, interaction: discord.Interaction):
        """处理分页按钮点击事件"""
        # 从按钮的 custom_id 中提取要切换到的页面
        page_to_switch = interaction.data['custom_id'].split('_')[-1]

        if self.current_page != page_to_switch:
            self.current_page = page_to_switch
            self.update_view() # 重新构建视图
            await interaction.response.edit_message(view=self)
        else:
            # 如果点的就是当前页，可以无响应或者给个提示
            await interaction.response.defer()

    async def on_refresh_button_click(self, interaction: discord.Interaction):
        """处理刷新按钮点击事件 (功能不变)"""
        await interaction.response.send_message(
            "📋 **关于数据刷新**\n"
            "此面板的数据由机器人每日凌晨自动更新。\n"
            "此按钮仅用于展示最近一次的更新时间，再次点击不会触发即时刷新哦。",
            ephemeral=True
        )


class ForumSelectView(ui.View):
    """
    这个视图保持不变，它的职责是选择频道并触发面板的创建。
    """
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

            # 创建新的分页视图实例
            view = StatisticsContainerView(stats_data=stats_data)
            msg = await interaction.channel.send(view=view)
            await statistics_db.add_statistics_panel(msg.id, msg.channel.id, channel.id, interaction.guild.id)
            await asyncio.sleep(1)

        await interaction.edit_original_response(content="✅ 操作完成。", view=None)
        self.stop()