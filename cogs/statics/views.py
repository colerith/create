import discord
from discord import ui
from datetime import datetime

from . import utils
from config import TZ_SHANGHAI

class StatisticsContainerView(ui.LayoutView):
    """
    展示频道统计数据的 Container 视图。
    """
    def __init__(self, target_forum: discord.ForumChannel, stats: dict):
        super().__init__(timeout=None) # 持久化视图
        self.target_forum = target_forum
        self.stats = stats

        # --- 刷新按钮 ---
        self.btn_refresh = ui.Button(
            label="手动刷新",
            style=discord.ButtonStyle.secondary,
            emoji="🔄",
            custom_id=f"stats_refresh_{target_forum.id}" # 加上唯一ID以防冲突
        )
        self.btn_refresh.callback = self.refresh_callback

        # 重新构建整个视图
        self.build_container()

    def build_container(self):
        """根据 stats 数据构建或重建 Container"""
        self.clear_items()

        elements = []

        # --- 1. 标题区 ---
        elements.append(ui.Section(
            ui.TextDisplay(content=f"### 📊 频道统计 · {self.target_forum.name}"),
            ui.TextDisplay(content=f"-# 深入洞察频道的活跃趋势与内容价值。"),
            accessory=ui.Thumbnail(media=self.target_forum.guild.icon.url if self.target_forum.guild.icon else None)
        ))

        # --- 2. 概览数据 ---
        elements.append(ui.Section(
            ui.TextDisplay(content=f"**帖子总数**: `{self.stats.get('total_count', 0)}`"),
            ui.TextDisplay(content=f"**近7日新增**: `{self.stats.get('weekly_count', 0)}`"),
            accessory=ui.Button(label="数据概览", style=discord.ButtonStyle.success, disabled=True)
        ))

        elements.append(ui.Separator())

        # --- 3. 热门帖子 ---
        elements.append(ui.TextDisplay(content="### 🔥 近期热门"))
        hot_threads = self.stats.get('hot_threads', [])
        if not hot_threads:
            elements.append(ui.TextDisplay(content="*暂无足够数据以生成热门榜单。*"))
        else:
            for thread in hot_threads:
                elements.append(ui.Section(
                    ui.TextDisplay(content=f"**{thread.name}**"),
                    ui.TextDisplay(content=f"-# 💬 {thread.message_count} 回复"),
                    accessory=ui.Button(label="直达", url=thread.jump_url, style=discord.ButtonStyle.link)
                ))

        elements.append(ui.Separator(spacing=discord.SeparatorSpacing.large))

        # --- 4. 冷门好帖 ---
        elements.append(ui.TextDisplay(content="### 🧊 冷门遗珠"))
        cold_gems = self.stats.get('cold_gems', [])
        if not cold_gems:
            elements.append(ui.TextDisplay(content="*这里很温暖，没有被冷落的帖子！*"))
        else:
            for thread in cold_gems:
                elements.append(ui.Section(
                    ui.TextDisplay(content=f"**{thread.name}**"),
                    ui.TextDisplay(content=f"-# 帖子发布于 {discord.utils.format_dt(thread.created_at, 'R')}"),
                    accessory=ui.Button(label="考古", url=thread.jump_url, style=discord.ButtonStyle.link, emoji="⛏️")
                ))

        # --- 5. 底部与刷新 ---
        elements.append(ui.Separator())
        last_updated_ts = discord.utils.format_dt(datetime.now(TZ_SHANGHAI))
        elements.append(ui.TextDisplay(content=f"数据更新于: {last_updated_ts}"))
        elements.append(ui.ActionRow(self.btn_refresh))

        container = ui.Container(
            *elements,
            accent_colour=discord.Color.from_rgb(88, 101, 242) # Discord Blurple
        )
        self.add_item(container)

    async def refresh_callback(self, interaction: discord.Interaction):
        """按钮点击的回调"""
        await interaction.response.defer(ephemeral=True, thinking=True)

        # 重新获取数据
        self.stats = await utils.fetch_forum_stats(self.target_forum)

        # 重建 Container
        self.build_container()

        # 编辑原消息
        await interaction.message.edit(view=self)
        await interaction.followup.send("✅ 面板已刷新！", ephemeral=True)


class ForumSelectView(ui.View):
    """
    用于让用户选择要生成统计面板的论坛频道。
    """
    def __init__(self, bot):
        super().__init__(timeout=300)
        self.bot = bot
        self.channel_select = ui.ChannelSelect(
            placeholder="请选择一个或多个论坛频道...",
            channel_types=[discord.ChannelType.forum],
            min_values=1,
            max_values=10 # 一次最多生成10个，防止刷屏
        )
        self.add_item(self.channel_select)

    @ui.button(label="生成统计面板", style=discord.ButtonStyle.primary, emoji="🚀")
    async def confirm_button(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)

        selected_forums = self.channel_select.values
        if not selected_forums:
            await interaction.followup.send("❌ 你没有选择任何频道。", ephemeral=True)
            return

        sent_count = 0
        for forum in selected_forums:
            # 确保获取到的是完整的 ForumChannel 对象
            full_forum = interaction.guild.get_channel(forum.id)
            if not isinstance(full_forum, discord.ForumChannel):
                continue

            stats = await utils.fetch_forum_stats(full_forum)
            view = StatisticsContainerView(full_forum, stats)

            try:
                msg = await interaction.channel.send(view=view)
                # 记录到数据库
                from . import db
                await db.add_panel_record(msg.id, msg.channel.id, msg.guild.id, full_forum.id)
                sent_count += 1
            except discord.Forbidden:
                await interaction.followup.send(f"❌ 我没有权限在 {interaction.channel.mention} 中发送消息。", ephemeral=True)
                return
            except Exception as e:
                print(f"发送统计面板时出错: {e}")

        await interaction.followup.send(f"✅ 成功生成了 {sent_count} 个频道的统计面板！", ephemeral=True)
        # 禁用原消息的按钮
        self.confirm_button.disabled = True
        await interaction.edit_original_response(view=self)