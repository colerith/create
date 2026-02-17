#cogs/exploration/views.py

import discord
from discord import app_commands, ui
from datetime import datetime

from cog import chimidan_text, execute_search, get_card_forums

# ==========================================
# Part 1. 通用分页视图
# ==========================================

class PaginatorView(ui.View):
    def __init__(self, data_list, title, is_daily=False, tz_info=None):
        super().__init__(timeout=None)
        self.data_list = data_list
        self.title = title
        self.is_daily = is_daily
        self.tz_info = tz_info
        self.per_page = 10
        self.current_page = 0
        self.total_pages = (len(data_list) - 1) // self.per_page + 1 if data_list else 1
        self.update_buttons()

    def update_buttons(self):
        self.prev_btn.disabled = (self.current_page == 0)
        self.next_btn.disabled = (self.current_page >= self.total_pages - 1)
        self.page_counter.label = f"第 {self.current_page + 1} / {self.total_pages} 页"

    def get_embed(self):
        start = self.current_page * self.per_page
        end = start + self.per_page
        page_items = self.data_list[start:end]

        desc_text = ""
        if self.is_daily:
            if not self.data_list:
                desc_text = chimidan_text("今天好安静唷，还没有新帖子捏... 🈚️")
            else:
                desc_text = chimidan_text(f"哇！今天全服新增了 {len(self.data_list)} 个有趣的帖子！")
        else:
            if not self.data_list:
                desc_text = chimidan_text("没有找到相关结果捏...")

        embed = discord.Embed(title=self.title, description=desc_text, color=0xffa07a if self.is_daily else 0x98fb98)

        for thread in page_items:
            author_name = thread.owner.display_name if thread.owner else "神秘蛋"
            category_name = thread.parent.name if thread.parent else "未知分区"
            tags_str = ""
            if thread.applied_tags:
                tags_str = " | ".join([f"🏷️{t.name}" for t in thread.applied_tags[:3]])
                tags_str = f"\n{tags_str}"

            embed.add_field(
                name=f"📄 {thread.name}",
                value=f"👤 作者: {author_name}\n📂 分区: {category_name}{tags_str}\n🔗 [点击跳转]({thread.jump_url})",
                inline=False
            )

        if self.is_daily:
            time_str = datetime.now(self.tz_info).strftime('%H:%M')
            embed.set_footer(text=f"最后更新于: {time_str} (每10分钟刷新)")
        else:
            embed.set_footer(text=f"共找到 {len(self.data_list)} 个结果 | 翻页看更多来捉")
        return embed

    @ui.button(emoji="⬅️", style=discord.ButtonStyle.secondary, custom_id="paginator_prev")
    async def prev_btn(self, interaction: discord.Interaction, button: ui.Button):
        if self.current_page > 0:
            self.current_page -= 1
            self.update_buttons()
            await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @ui.button(label="1/1", style=discord.ButtonStyle.gray, disabled=True, custom_id="paginator_count")
    async def page_counter(self, interaction: discord.Interaction, button: ui.Button):
        pass

    @ui.button(emoji="➡️", style=discord.ButtonStyle.secondary, custom_id="paginator_next")
    async def next_btn(self, interaction: discord.Interaction, button: ui.Button):
        if self.current_page < self.total_pages - 1:
            self.current_page += 1
            self.update_buttons()
            await interaction.response.edit_message(embed=self.get_embed(), view=self)


# ==========================================
# Part 2. 搜索 UI 组件
# ==========================================

class TagSelect(ui.Select):
    def __init__(self, tags):
        options = [discord.SelectOption(label=tag.name, value=str(tag.id), emoji=tag.emoji or "🏷️") for tag in tags[:25]]
        super().__init__(placeholder="[可选] 进一步筛选标签 (多选)", min_values=0, max_values=len(options), options=options, row=1)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()

class ChannelFilterView(ui.View):
    def __init__(self, search_type: str, query_data):
        super().__init__(timeout=None)
        self.search_type = search_type
        self.query_data = query_data
        self.selected_tags = []

        self.channel_select = ui.ChannelSelect(placeholder="[可选] 选择特定的论坛分区...", channel_types=[discord.ChannelType.forum], min_values=0, max_values=25, row=0)
        self.channel_select.callback = self.on_channel_select
        self.add_item(self.channel_select)

    async def on_channel_select(self, interaction: discord.Interaction):
        selected_channels = self.channel_select.values
        for item in self.children[:]:
            if isinstance(item, TagSelect):
                self.remove_item(item)

        if len(selected_channels) == 1:
            channel = interaction.guild.get_channel(selected_channels[0].id)
            if isinstance(channel, discord.ForumChannel) and channel.available_tags:
                self.add_item(TagSelect(channel.available_tags))

        await interaction.response.edit_message(view=self)

    @ui.button(label="开始搜索", style=discord.ButtonStyle.primary, row=2, emoji="🔎")
    async def confirm_search(self, interaction: discord.Interaction, button: ui.Button):
        selected_tag_ids = []
        for item in self.children:
            if isinstance(item, TagSelect):
                selected_tag_ids = item.values

        # 调用从 cog.py 导入的搜索执行函数
        await execute_search(interaction, self.search_type, self.query_data, self.channel_select.values, selected_tag_ids)

class KeywordInputModal(ui.Modal, title="关键词搜索"):
    keyword = ui.TextInput(label="关键词", placeholder="请输入帖子标题或内容关键词...", min_length=1)
    async def on_submit(self, interaction: discord.Interaction):
        view = ChannelFilterView(search_type="keyword", query_data=self.keyword.value)
        await interaction.response.send_message(
            chimidan_text(f"关键词“{self.keyword.value}”记录下来惹！\n请选择搜索范围："),
            view=view, ephemeral=True
        )

class UserSelectView(ui.View):
    def __init__(self): super().__init__(timeout=None)
    @ui.select(cls=ui.UserSelect, placeholder="选择帖子的作者...", min_values=1, max_values=1)
    async def select_user(self, interaction: discord.Interaction, select: ui.UserSelect):
        view = ChannelFilterView(search_type="user", query_data=select.values[0])
        await interaction.response.send_message(
            chimidan_text(f"原来是找 {select.values[0].display_name} 嘟帖子...\n请选择搜索范围："),
            view=view, ephemeral=True
        )

class SearchMethodView(ui.View):
    def __init__(self): super().__init__(timeout=None)

    @ui.button(label="按关键词搜索", style=discord.ButtonStyle.success, emoji="📝", custom_id="search_panel_btn_keyword")
    async def by_keyword(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(KeywordInputModal())

    @ui.button(label="按用户搜索", style=discord.ButtonStyle.primary, emoji="👤", custom_id="search_panel_btn_user")
    async def by_user(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message(
            chimidan_text("请选择你要查找的用户来捉："),
            view=UserSelectView(), ephemeral=True
        )
