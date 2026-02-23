# cogs/recommend/views.py

import discord
from discord import ui
import random

from . import db
from . import utils
from config import TEST_ROLE_ID

# =================================================================
#  Gacha System (抽卡系统) - 使用 Container 布局
# =================================================================

class GachaContainerView(ui.LayoutView):
    """
    抽卡控制台 + 结果展示
    """
    def __init__(self, guild_forums: list[discord.ForumChannel], user: discord.User):
        super().__init__(timeout=300)
        self.user = user
        self.guild_forums = guild_forums
        self.selected_channel_id = None

        # --- 组件定义 ---

        # 1. 频道选择器
        options = [discord.SelectOption(label="🌐 全部分区 (默认)", value="all")]
        # 限制数量，Discord Select 最大 25
        for forum in guild_forums[:24]:
            options.append(discord.SelectOption(label=f"📂 {forum.name}", value=str(forum.id)))

        self.channel_select = ui.Select(
            placeholder="[可选] 筛选特定资源池...",
            options=options,
            min_values=1,
            max_values=1,
            custom_id="gacha_channel_select"
        )
        self.channel_select.callback = self.on_select_change

        # 2. 抽卡按钮
        self.btn_draw_one = ui.Button(label="单抽", style=discord.ButtonStyle.primary, emoji="1️⃣")
        self.btn_draw_one.callback = lambda i: self.execute_draw(i, 1)

        self.btn_draw_five = ui.Button(label="五连", style=discord.ButtonStyle.secondary, emoji="5️⃣")
        self.btn_draw_five.callback = lambda i: self.execute_draw(i, 5)

        self.btn_draw_ten = ui.Button(label="十连", style=discord.ButtonStyle.success, emoji="🔟")
        self.btn_draw_ten.callback = lambda i: self.execute_draw(i, 10)

        # 3. 初始显示内容
        init_accessory = ui.Thumbnail(media=self.user.display_avatar.url)

        self.display_section = ui.Section(
            ui.TextDisplay(content="### 🎴 缘分感应控制台"),
            ui.TextDisplay(content="请选择资源池，然后点击按钮开始抽卡。"),
            ui.TextDisplay(content="-# 每天仅限 1 次机会 (测试员除外)"),
            accessory=init_accessory # ✅ 必填
        )

        # 4. 结果展示区域 (初始为空或占位)
        self.result_section = ui.Section(
            ui.TextDisplay(content="*等待抽卡结果...*")
        )

        # --- 构建初始 Container ---
        self.update_container()

    def update_container(self, result_content=None, result_embeds=None):
        """重新构建 Container"""
        self.clear_items()

        # 顶部提示区
        pool_name = "全部分区"
        if self.selected_channel_id:
            f = next((f for f in self.guild_forums if f.id == self.selected_channel_id), None)
            pool_name = f.name if f else "未知分区"

        user_avatar = ui.Thumbnail(media=self.user.display_avatar.url)

        header_section = ui.Section(
            ui.TextDisplay(content="### 🎴 缘分感应控制台"),
            ui.TextDisplay(content=f"**当前卡池:** {pool_name}"),
            ui.TextDisplay(content="-# 每天仅限 1 次机会 (测试员除外)"),
            accessory=user_avatar # ✅ 必填
        )

        # 动作区
        action_rows = [
            ui.ActionRow(self.channel_select),
            ui.ActionRow(self.btn_draw_one, self.btn_draw_five, self.btn_draw_ten)
        ]

        # 结果区 (如果有)
        elements = [header_section, ui.Separator()]

        if result_content:
            # 简单的结果标题
            elements.append(ui.TextDisplay(content="### ✨ 感应结果"))

            # 处理结果内容 (如果是列表)
            if isinstance(result_content, list):
                # 如果是多条结果，我们可能需要用多个 Section 或拼合文本
                # 简单起见，拼合文本
                desc = "\n".join(result_content)
                elements.append(ui.TextDisplay(content=desc))
            else:
                elements.append(ui.TextDisplay(content=str(result_content)))

        else:
             elements.append(ui.TextDisplay(content="*等待抽卡...*"))


        container = ui.Container(
            *elements,
            *action_rows,
            accent_colour=discord.Color.from_rgb(255, 105, 180) # Hot Pink
        )
        self.add_item(container)

    async def on_select_change(self, interaction: discord.Interaction):
        val = self.channel_select.values[0]
        self.selected_channel_id = int(val) if val != "all" else None

        # 刷新界面显示当前卡池
        self.update_container()
        await interaction.response.edit_message(view=self)

    async def execute_draw(self, interaction: discord.Interaction, count: int):
        is_tester = isinstance(interaction.user, discord.Member) and bool(interaction.user.get_role(TEST_ROLE_ID))

        if not is_tester and await db.check_user_drawn_today(interaction.user.id):
            return await interaction.response.send_message("🔮 您今天已经感应过缘分啦，明天再来吧！", ephemeral=True)

        await interaction.response.defer()

        threads = await utils.get_random_thread_pool(interaction.guild, self.selected_channel_id)
        if not threads:
            return await interaction.followup.send("🏜️ 卡池里空空如也...", ephemeral=True)

        drawn_threads = random.sample(threads, min(count, len(threads)))

        if not is_tester:
            await db.mark_user_drawn(interaction.user.id)

        # 生成结果内容
        result_lines = []
        if len(drawn_threads) == 1:
            info = await utils.fetch_thread_details(drawn_threads[0])
            result_lines.append(f"**[{info['title']}]({info['url']})**")
            result_lines.append(f"👤 {info['author_mention']} | 📂 {info['category']}")
            result_lines.append(f"-# {info['intro'][:100]}...") # 截断简介
        else:
            result_lines.append(f"💫 **恭喜获得 {len(drawn_threads)} 连抽结果！**")
            for i, t in enumerate(drawn_threads):
                result_lines.append(f"{i+1}. [{t.name}]({t.jump_url})")

        # 更新容器显示结果
        self.update_container(result_content=result_lines)
        await interaction.edit_original_response(view=self)

# =================================================================
#  Daily Recommendation (每日推荐) - 使用 Container 布局
# =================================================================

class DailyRecommendContainer(ui.LayoutView):
    def __init__(self, thread_info: dict, is_empty=False):
        super().__init__(timeout=None) # 持久化视图，无超时

        self.btn_gacha = ui.Button(
            label="🔮 抽取今日缘分",
            style=discord.ButtonStyle.primary,
            custom_id="daily_gacha_open_btn"
        )
        self.btn_gacha.callback = self.open_gacha

        # 构建容器
        if is_empty:
             # 创建一个禁用的按钮作为占位 accessory
             empty_accessory = ui.Button(label="暂无", disabled=True, style=discord.ButtonStyle.secondary)

             container = ui.Container(
                ui.Section(
                    ui.TextDisplay(content="### 📅 每日推荐"),
                    ui.TextDisplay(content="今天资源库里空空如也..."),
                    accessory=empty_accessory
                ),
                accent_colour=discord.Color.light_grey()
            )
        else:
            # 动态生成 Section

            # 1. 标题与作者区
            header_section = ui.Section(
                ui.TextDisplay(content=f"### 📅 每日精选 · {thread_info['title']}"),
                ui.TextDisplay(content=f"👤 作者: {thread_info['author_mention']}"),
                # 这里原逻辑已有 accessory
                accessory=ui.Button(label="跳转原帖", url=thread_info['url'], style=discord.ButtonStyle.link)
            )

            # 2. 简介区
            clean_intro = thread_info['intro'][:200] + "..." if len(thread_info['intro']) > 200 else thread_info['intro']

            components = [header_section]

            # 如果有预览图，使用 MediaGallery
            if thread_info['image']:
                components.append(
                    ui.MediaGallery(
                        discord.MediaGalleryItem(media=thread_info['image'])
                    )
                )

            # 简介放在中间或图下面
            components.append(
                ui.Section(
                    ui.TextDisplay(content="**简介:**"),
                    ui.TextDisplay(content=clean_intro),
                    accessory=ui.Thumbnail(media=thread_info['author_avatar'] or thread_info['image'] or "https://cdn.discordapp.com/embed/avatars/0.png")
                )
            )

            # 底部信息
            tags_str = " / ".join(thread_info['tags'][:5]) # 最多显示5个标签
            components.append(
                 ui.Section(
                    ui.TextDisplay(content=f"📂 **分区**: {thread_info['category']}"),
                    ui.TextDisplay(content=f"🏷️ **标签**: {tags_str}"),
                    accessory=ui.Button(label="查看详情", url=thread_info['url'], style=discord.ButtonStyle.secondary, disabled=True)
                )
            )

            # 按钮区 (ActionRow)
            components.append(ui.Separator())
            components.append(ui.ActionRow(self.btn_gacha))

            container = ui.Container(
                *components,
                accent_colour=discord.Color.from_rgb(255, 105, 180) # Pink
            )

        self.add_item(container)

    async def open_gacha(self, interaction: discord.Interaction):
        forums = utils.get_card_forums(interaction.guild)
        if not forums:
            return await interaction.response.send_message("本服未配置资源频道。", ephemeral=True)

        view = GachaContainerView(forums, interaction.user)
        await interaction.response.send_message(view=view, ephemeral=True)
