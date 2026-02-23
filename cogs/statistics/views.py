import discord
from discord import ui
from datetime import datetime

class StatisticsContainerView(ui.LayoutView):
    def __init__(self, stats_data: dict):
        super().__init__(timeout=None)

        # --- 组件定义 ---
        self.btn_refresh_info = ui.Button(
            label=f"数据更新于 {datetime.now().strftime('%H:%M:%S')}",
            style=discord.ButtonStyle.secondary,
            disabled=True,
            custom_id="stats_panel_info_btn" # 固定的custom_id用于持久化
        )

        # --- 构建容器 ---
        self.clear_items()

        # 1. 频道标题与总体数据
        header_section = ui.Section(
            ui.TextDisplay(content=f"### 📊 频道统计: {stats_data['channel_name']}"),
            ui.TextDisplay(content=f"**总帖子数:** {stats_data['total_threads']} | **近7日新增:** {stats_data['new_threads_7d']}"),
            accessory=ui.Thumbnail(media=stats_data.get('channel_icon_url', "https://cdn.discordapp.com/embed/avatars/0.png"))
        )

        elements = [header_section]

        # 2. 热门帖子列表
        hot_threads = stats_data.get('hot_threads', [])
        if hot_threads:
            # 在TextDisplay的content中直接使用Markdown链接
            hot_content = "\n".join(
                [f"[`👍{th['likes']} | 💬{th['comments']}`] [{th['name']}]({th['url']})" for th in hot_threads]
            )
            elements.extend([
                ui.Separator(),
                ui.TextDisplay(content="#### 🔥 热门帖子"),
                ui.TextDisplay(content=hot_content if hot_content else "暂无数据")
            ])

        # 3. 冷门宝藏列表
        cold_threads = stats_data.get('cold_threads', [])
        if cold_threads:
            cold_content = "\n".join(
                [f"[`👍{th['likes']} | 💬{th['comments']}`] [{th['name']}]({th['url']})" for th in cold_threads]
            )
            elements.extend([
                ui.Separator(),
                ui.TextDisplay(content="#### 💎 冷门宝藏"),
                ui.TextDisplay(content=cold_content if cold_content else "暂无数据")
            ])

        # 4. 底部信息行
        elements.extend([
            ui.Separator(spacing=discord.SeparatorSpacing.large),
            ui.ActionRow(self.btn_refresh_info)
        ])

        container = ui.Container(
            *elements,
            accent_colour=discord.Color.from_rgb(88, 101, 242) # Discord Blurple
        )
        self.add_item(container)