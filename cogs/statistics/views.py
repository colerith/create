import discord
from discord import ui
from datetime import datetime
from . import db as statistics_db
import asyncio

# 导入时区配置
from config import TZ_SHANGHAI


class ForumChannelMultiSelect(ui.ChannelSelect):
    def __init__(self):
        super().__init__(
            placeholder="请选择1-5个论坛频道...",
            channel_types=[discord.ChannelType.forum],
            min_values=1,
            max_values=5,
            custom_id="stats_forum_selector",
        )

    async def callback(self, interaction: discord.Interaction):
        selected_count = len(self.values)
        if self.view and hasattr(self.view, "selected_channel_ids"):
            self.view.selected_channel_ids = [channel.id for channel in self.values]
        if self.view and hasattr(self.view, "update_confirm_button"):
            self.view.update_confirm_button()
        if self.view:
            await interaction.response.edit_message(
                content=f"已选择 **{selected_count}** 个论坛频道，点击下方按钮开始生成统计面板。",
                view=self.view,
            )


class StatisticsContainerView(ui.LayoutView):
    """
    【最终版分页视图】
    - 显示作者和标签
    - 使用东八区时间
    """

    def __init__(
        self,
        stats_data: dict,
        cog_instance=None,
        panel_message_id: int | None = None,
        forum_channel_id: int | None = None,
        current_page: str = "hot",
    ):
        super().__init__(timeout=None)
        self.stats_data = stats_data
        self.cog = cog_instance
        self.panel_message_id = panel_message_id
        self.forum_channel_id = forum_channel_id or stats_data.get("forum_channel_id")
        self.current_page = current_page
        self.update_view()

    def update_view(self):
        self.clear_items()
        elements = []
        MAX_THREADS_PER_PAGE = 5

        # === 1. 静态顶部内容 (不变) ===
        elements.append(
            ui.Section(
                ui.TextDisplay(
                    content=f"### 📊 频道统计 · {self.stats_data.get('channel_name', '加载中')}"
                ),
                ui.TextDisplay(content="😋来看看有什么好帖子吧！"),
                accessory=ui.Thumbnail(
                    media=self.stats_data.get(
                        "channel_icon_url",
                        "https://upload.wikimedia.org/wikipedia/commons/c/ca/1x1.png",
                    )
                ),
            )
        )
        elements.append(
            ui.Section(
                ui.TextDisplay(
                    content=f"**总帖子数:** {self.stats_data.get('total_threads', 0)}"
                ),
                ui.TextDisplay(
                    content=f"**近7日新增:** {self.stats_data.get('new_threads_7d', 0)}"
                ),
                accessory=ui.Button(
                    label="数据概览",
                    style=discord.ButtonStyle.success,
                    disabled=True,
                    custom_id="stats_overview_placeholder",
                ),
            )
        )
        elements.append(ui.Separator())

        # === 2. 页面切换按钮 (不变) ===
        self.btn_hot = ui.Button(
            label="🔥 近期热门",
            style=discord.ButtonStyle.primary,
            custom_id="stats_page_hot",
            disabled=(self.current_page == "hot"),
        )
        self.btn_hot.callback = self.on_page_switch
        self.btn_cold = ui.Button(
            label="💎 冷门遗珠",
            style=discord.ButtonStyle.primary,
            custom_id="stats_page_cold",
            disabled=(self.current_page == "cold"),
        )
        self.btn_cold.callback = self.on_page_switch
        elements.append(ui.ActionRow(self.btn_hot, self.btn_cold))

        # === 3. 动态内容区 (核心修改) ===
        if self.current_page == "hot":
            hot_threads = self.stats_data.get("hot_threads", [])
            if hot_threads:
                for th in hot_threads[:MAX_THREADS_PER_PAGE]:
                    # 【新增】处理标签显示
                    tags_str = " · ".join(th.get("tags", []))
                    if tags_str:
                        tags_str = f" · {tags_str}"  # 加个分隔符

                    info_line = (
                        f"**{th.get('name', '无标题')[:60]}**\n"
                        # 【修改】添加作者、点赞、评论和标签
                        f"-# 👤 {th.get('author_name', '佚名')} · 👍 {th.get('likes', 0)} · 💬 {th.get('comments', 0)}{tags_str}"
                    )
                    elements.append(
                        ui.Section(
                            ui.TextDisplay(content=info_line),
                            accessory=ui.Button(
                                label="直达",
                                style=discord.ButtonStyle.secondary,
                                url=th.get("url"),
                            ),
                        )
                    )
            else:
                elements.append(ui.TextDisplay(content="-# 暂无热门帖子..."))

        elif self.current_page == "cold":
            cold_threads = self.stats_data.get("cold_threads", [])
            if cold_threads:
                for th in cold_threads[:MAX_THREADS_PER_PAGE]:
                    relative_time = (
                        discord.utils.format_dt(th["created_at"], style="R")
                        if th.get("created_at")
                        else "未知"
                    )
                    # 【新增】处理标签显示
                    tags_str = " · ".join(th.get("tags", []))
                    if tags_str:
                        tags_str = f" · {tags_str}"

                    info_line = (
                        f"**{th.get('name', '无标题')[:60]}**\n"
                        # 【修改】添加作者、发布时间和标签
                        f"-# 👤 {th.get('author_name', '佚名')} · 发布于 {relative_time}{tags_str}"
                    )
                    elements.append(
                        ui.Section(
                            ui.TextDisplay(content=info_line),
                            accessory=ui.Button(
                                label="考古",
                                style=discord.ButtonStyle.secondary,
                                url=th.get("url"),
                            ),
                        )
                    )
            else:
                elements.append(ui.TextDisplay(content="-# 暂无冷门帖子..."))

        # === 4. 静态底部内容 (核心修改) ===
        elements.append(ui.Separator(spacing=discord.SeparatorSpacing.large))

        # 【修改】使用带时区的时间
        update_time_str = datetime.now(TZ_SHANGHAI).strftime("%H:%M")
        self.btn_refresh_info = ui.Button(
            label=f"数据更新于 {update_time_str} (UTC+8)",
            style=discord.ButtonStyle.secondary,
            custom_id=f"stats_manual_refresh_btn:{self.forum_channel_id or 0}",
            emoji="🔄",
        )
        self.btn_refresh_info.callback = self.on_refresh_button_click
        elements.append(ui.ActionRow(self.btn_refresh_info))

        # --- 最终组合 ---
        container = ui.Container(
            *elements, accent_colour=discord.Color.from_rgb(113, 135, 212)
        )
        self.add_item(container)

    async def on_page_switch(self, interaction: discord.Interaction):
        page_to_switch = interaction.data["custom_id"].split("_")[-1]
        if self.current_page != page_to_switch:
            self.current_page = page_to_switch
            self.update_view()
            await interaction.response.edit_message(view=self)
        else:
            await interaction.response.defer()

    async def on_refresh_button_click(self, interaction: discord.Interaction):
        if not self.cog:
            return await interaction.response.send_message(
                "统计面板刷新实例未注册，暂时无法使用该按钮。", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True, thinking=True)
        forum_channel_id = self.forum_channel_id
        custom_id = interaction.data.get("custom_id") if interaction.data else None
        if not forum_channel_id and custom_id and ":" in custom_id:
            _, forum_id_raw = custom_id.split(":", 1)
            if forum_id_raw.isdigit() and int(forum_id_raw) > 0:
                forum_channel_id = int(forum_id_raw)

        guild = interaction.guild
        forum_channel = (
            guild.get_channel(forum_channel_id) if guild and forum_channel_id else None
        )

        if not isinstance(forum_channel, discord.ForumChannel):
            panel_info = await statistics_db.get_statistics_panel(
                interaction.message.id
            )
            if panel_info:
                guild = interaction.client.get_guild(panel_info["guild_id"])
                forum_channel = (
                    guild.get_channel(panel_info["forum_channel_id"]) if guild else None
                )
                forum_channel_id = panel_info["forum_channel_id"]

        if not isinstance(forum_channel, discord.ForumChannel):
            return await interaction.followup.send(
                "找不到这个统计面板关联的论坛频道，无法刷新。", ephemeral=True
            )

        self.forum_channel_id = forum_channel_id

        stats_data = await self.cog.gather_statistics(forum_channel)
        if not stats_data:
            return await interaction.followup.send(
                "刷新失败，无法读取该论坛的数据。", ephemeral=True
            )

        refreshed_view = StatisticsContainerView(
            stats_data=stats_data,
            cog_instance=self.cog,
            panel_message_id=interaction.message.id,
            forum_channel_id=forum_channel_id,
            current_page=self.current_page,
        )
        await interaction.message.edit(view=refreshed_view)
        await interaction.followup.send("✅ 统计面板已立即刷新。", ephemeral=True)


class ForumSelectView(ui.View):
    """
    这个视图保持不变。
    """

    def __init__(self, cog_instance):
        super().__init__(timeout=180)
        self.cog = cog_instance
        self.selected_channel_ids = []
        self.channel_select = ForumChannelMultiSelect()
        self.add_item(self.channel_select)
        self.update_confirm_button()

    def update_confirm_button(self):
        for item in self.children:
            if (
                isinstance(item, ui.Button)
                and item.custom_id == "stats_confirm_generation_btn"
            ):
                item.label = f"生成统计面板 ({len(self.selected_channel_ids)})"
                item.disabled = len(self.selected_channel_ids) == 0
                break

    @ui.button(
        label="生成统计面板",
        style=discord.ButtonStyle.success,
        custom_id="stats_confirm_generation_btn",
    )
    async def confirm_button(self, interaction: discord.Interaction, button: ui.Button):
        if not self.selected_channel_ids:
            return await interaction.response.send_message(
                "你还没有选择任何频道！", ephemeral=True
            )

        selected_channels = [
            interaction.guild.get_channel(channel_id)
            for channel_id in self.selected_channel_ids
        ]
        selected_channels = [
            channel
            for channel in selected_channels
            if isinstance(channel, discord.ForumChannel)
        ]
        if not selected_channels:
            return await interaction.response.send_message(
                "选中的论坛频道已失效，请重新选择。", ephemeral=True
            )

        button.disabled = True
        self.channel_select.disabled = True
        await interaction.response.edit_message(
            content=f"⏳ 开始为 **{len(selected_channels)}** 个频道生成统计面板...",
            view=self,
        )

        created_channels = []
        failed_channels = []
        for channel in selected_channels:
            stats_data = await self.cog.gather_statistics(channel)
            if stats_data is None:
                failed_channels.append(channel.mention)
                continue

            view = StatisticsContainerView(
                stats_data=stats_data,
                cog_instance=self.cog,
                forum_channel_id=channel.id,
            )
            msg = await interaction.channel.send(view=view)
            await statistics_db.add_statistics_panel(
                msg.id, msg.channel.id, channel.id, interaction.guild.id
            )
            refreshed_view = StatisticsContainerView(
                stats_data=stats_data,
                cog_instance=self.cog,
                panel_message_id=msg.id,
                forum_channel_id=channel.id,
            )
            await msg.edit(view=refreshed_view)
            created_channels.append(channel.mention)
            await asyncio.sleep(1)

        result_lines = [f"✅ 已成功生成 **{len(created_channels)}** 个统计面板。"]
        if created_channels:
            result_lines.append("已生成: " + "、".join(created_channels))
        if failed_channels:
            result_lines.append("失败: " + "、".join(failed_channels))

        await interaction.edit_original_response(
            content="\n".join(result_lines), view=None
        )
        self.stop()
