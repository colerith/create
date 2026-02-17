#cogs/recommend/views.py

import discord
from discord import ui

class GachaControlView(ui.View):
    def __init__(self, cog, guild_forums):
        super().__init__(timeout=None)
        self.cog = cog # 保存对主 Cog 的引用，以便调用其方法
        self.selected_channel_id = None

        options = [discord.SelectOption(label="🌐 全部分区 (默认)", value="all", description="从所有资源分区随机抽取")]
        for forum in guild_forums[:24]:
            options.append(discord.SelectOption(label=f"📂 {forum.name}", value=str(forum.id)))

        self.channel_select = ui.Select(placeholder="[可选] 筛选特定资源池...", options=options, min_values=1, max_values=1, row=0)
        self.channel_select.callback = self.on_select_change
        self.add_item(self.channel_select)

    async def on_select_change(self, interaction: discord.Interaction):
        val = self.channel_select.values[0]
        self.selected_channel_id = int(val) if val != "all" else None

        pool_name = "全部分区"
        if self.selected_channel_id:
            ch = interaction.guild.get_channel(self.selected_channel_id)
            pool_name = ch.name if ch else "未知分区"

        await interaction.response.edit_message(content=f"🎯 当前卡池已锁定：**{pool_name}**\n请点击下方按钮开始抽取！(注意：每天只能抽一次哦)", view=self)

    @ui.button(label="单抽 (1发)", style=discord.ButtonStyle.primary, row=1, emoji="1️⃣")
    async def draw_one(self, interaction: discord.Interaction, button: ui.Button):
        await self.cog.execute_draw(interaction, count=1, channel_id=self.selected_channel_id)

    @ui.button(label="五连抽 (5发)", style=discord.ButtonStyle.secondary, row=1, emoji="5️⃣")
    async def draw_five(self, interaction: discord.Interaction, button: ui.Button):
        await self.cog.execute_draw(interaction, count=5, channel_id=self.selected_channel_id)

    @ui.button(label="十连抽 (10发)", style=discord.ButtonStyle.success, row=1, emoji="🔟")
    async def draw_ten(self, interaction: discord.Interaction, button: ui.Button):
         await self.cog.execute_draw(interaction, count=10, channel_id=self.selected_channel_id)


class DailyRecommendView(ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog # 保存对主 Cog 的引用

    @ui.button(label="🔮 抽取今日缘分", style=discord.ButtonStyle.primary, custom_id="daily_gacha_open_btn")
    async def open_gacha(self, interaction: discord.Interaction, button: ui.Button):
        forums = self.cog.get_card_forums(interaction.guild)
        if not forums:
            return await interaction.response.send_message("本服务器没有配置相关资源频道，无法抽卡。", ephemeral=True)

        view = GachaControlView(self.cog, forums)

        await interaction.response.send_message(
            "🎴 **抽卡控制台已启动**\n请选择想要抽取的卡池（默认全部），然后点击抽卡按钮。\n*每天仅限抽取一次哦！*",
            view=view,
            ephemeral=True
        )