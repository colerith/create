# cogs/exploration/views.py

import discord
from discord import ui
from datetime import datetime
import asyncio

from config import TZ_SHANGHAI

# ==========================================
# Part 1. 核心搜索执行逻辑
# ==========================================

async def execute_search(interaction: discord.Interaction, search_type: str, query_data, selected_channels, selected_tag_ids=None):
    await interaction.response.send_message(
        "收到指令惹！正在全速启动搜索引擎... (0%)",
        ephemeral=True
    )

    target_forums = []
    if selected_channels:
        for ch in selected_channels:
            full_channel = interaction.guild.get_channel(ch.id)
            if full_channel and isinstance(full_channel, discord.ForumChannel):
                target_forums.append(full_channel)
    else:
        target_forums = [ch for ch in interaction.guild.forums if isinstance(ch, discord.ForumChannel)]

    all_threads = [t for forum in target_forums for t in forum.threads]

    if not all_threads:
        return await interaction.edit_original_response(content="呜呜，当前范围内没有帖子可以搜捏...")

    sem = asyncio.Semaphore(8)
    results = []
    processed_count = 0
    total_count = len(all_threads)
    target_tags_set = set(map(int, selected_tag_ids)) if selected_tag_ids else set()

    async def check_thread(thread):
        nonlocal processed_count
        try:
            async with sem:
                if target_tags_set and not ({tag.id for tag in thread.applied_tags} & target_tags_set):
                    return None

                if search_type == "user" and thread.owner_id == query_data.id: return thread
                elif search_type == "keyword":
                    keyword = query_data.lower()
                    if keyword in thread.name.lower(): return thread
                    try:
                        starter = thread.starter_message or (await thread.history(limit=1, oldest_first=True).flatten())[0]
                        if starter and starter.content and keyword in starter.content.lower(): return thread
                    except IndexError: pass # 帖子可能没有起始消息
        except Exception:
            return None
        finally:
            processed_count += 1
        return None


    tasks_list = [check_thread(t) for t in all_threads]
    last_update_time = datetime.now()

    for future in asyncio.as_completed(tasks_list):
        result = await future
        if result: results.append(result)

        now = datetime.now()
        if (now - last_update_time).total_seconds() > 1.5 or processed_count == total_count:
            percent = int((processed_count / total_count) * 100)
            try:
                await interaction.edit_original_response(
                    content=f"正在全速搜索中... 咻咻咻！\n进度：{percent}% ({processed_count}/{total_count})\n已找到：{len(results)} 个匹配"
                )
                last_update_time = now
            except discord.NotFound: break

    if not results:
        return await interaction.edit_original_response(content=f"呜呜，翻遍了 {total_count} 个帖子也没找到捏...")

    extra_info = f" (含标签筛选)" if selected_tag_ids else ""
    paginator = PaginatorView(results, title=f"🔍 搜索结果: {len(results)}条{extra_info}", is_daily=False)
    await interaction.edit_original_response(
        content="搜索完成惹！找到以下内容：",
        embed=paginator.get_embed(),
        view=paginator
    )


# ==========================================
# Part 2. UI 组件
# ==========================================

class PaginatorView(ui.View):
    def __init__(self, data_list, title, is_daily=False):
        super().__init__(timeout=None)
        self.data_list = data_list
        self.title = title
        self.is_daily = is_daily
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
            if not self.data_list: desc_text = "今天好安静唷，还没有新帖子捏... 🈚️"
            else: desc_text = f"哇！今天全服新增了 {len(self.data_list)} 个有趣的帖子！"
        else:
            if not self.data_list: desc_text = "没有找到相关结果捏..."

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
            time_str = datetime.now(TZ_SHANGHAI).strftime('%H:%M')
            embed.set_footer(text=f"最后更新于: {time_str} (每10分钟刷新)")
        else:
            embed.set_footer(text=f"共找到 {len(self.data_list)} 个结果 | 翻页看更多来捉")
        return embed

    @ui.button(emoji="⬅️", style=discord.ButtonStyle.secondary, custom_id="paginator_prev")
    async def prev_btn(self, i: discord.Interaction, button: ui.Button):
        if self.current_page > 0: self.current_page -= 1; self.update_buttons(); await i.response.edit_message(embed=self.get_embed(), view=self)

    @ui.button(label="1/1", style=discord.ButtonStyle.gray, disabled=True, custom_id="paginator_count")
    async def page_counter(self, i: discord.Interaction, button: ui.Button): pass

    @ui.button(emoji="➡️", style=discord.ButtonStyle.secondary, custom_id="paginator_next")
    async def next_btn(self, i: discord.Interaction, button: ui.Button):
        if self.current_page < self.total_pages - 1: self.current_page += 1; self.update_buttons(); await i.response.edit_message(embed=self.get_embed(), view=self)


class TagSelect(ui.Select):
    """动态生成的标签选择器"""
    def __init__(self, tags):
        options = [discord.SelectOption(label=tag.name, value=str(tag.id), emoji=tag.emoji) for tag in tags[:25]]
        super().__init__(placeholder="[可选] 进一步筛选标签 (多选)", min_values=0, max_values=len(options), options=options, row=1)
    async def callback(self, interaction: discord.Interaction): await interaction.response.defer()


class ChannelFilterView(ui.View):
    """频道与标签筛选视图"""
    def __init__(self, search_type: str, query_data):
        super().__init__(timeout=180)
        self.search_type = search_type
        self.query_data = query_data

        self.channel_select = ui.ChannelSelect(placeholder="[可选] 选择特定的论坛分区...", channel_types=[discord.ChannelType.forum], min_values=0, max_values=25, row=0)
        self.channel_select.callback = self.on_channel_select
        self.add_item(self.channel_select)

    async def on_channel_select(self, interaction: discord.Interaction):
        for item in self.children[:]:
            if isinstance(item, TagSelect): self.remove_item(item)

        selected_channels = self.channel_select.values
        if len(selected_channels) == 1:
            channel = interaction.guild.get_channel(selected_channels[0].id)
            if isinstance(channel, discord.ForumChannel) and channel.available_tags:
                self.add_item(TagSelect(channel.available_tags))
        await interaction.response.edit_message(view=self)

    @ui.button(label="开始搜索", style=discord.ButtonStyle.primary, row=2, emoji="🔎")
    async def confirm_search(self, interaction: discord.Interaction, button: ui.Button):
        selected_tag_ids = []
        for item in self.children:
            if isinstance(item, TagSelect): selected_tag_ids = item.values; break
        await execute_search(interaction, self.search_type, self.query_data, self.channel_select.values, selected_tag_ids)


class KeywordInputModal(ui.Modal, title="关键词搜索"):
    keyword = ui.TextInput(label="关键词", placeholder="请输入帖子标题或内容关键词...", min_length=1)
    async def on_submit(self, interaction: discord.Interaction):
        view = ChannelFilterView(search_type="keyword", query_data=self.keyword.value)
        await interaction.response.send_message(
            f"关键词“{self.keyword.value}”记录下来惹！\n请选择搜索范围：",
            view=view, ephemeral=True
        )


class UserSelectView(ui.View):
    def __init__(self): super().__init__(timeout=180)
    @ui.select(cls=ui.UserSelect, placeholder="选择帖子的作者...", min_values=1, max_values=1)
    async def select_user(self, interaction: discord.Interaction, select: ui.UserSelect):
        view = ChannelFilterView(search_type="user", query_data=select.values[0])
        await interaction.response.send_message(
            f"原来是找 {select.values[0].display_name} 嘟帖子...\n请选择搜索范围：",
            view=view, ephemeral=True
        )


class SearchMethodView(ui.View):
    """初始搜索方式选择视图"""
    def __init__(self): super().__init__(timeout=None)

    @ui.button(label="按关键词搜索", style=discord.ButtonStyle.success, emoji="📝", custom_id="search_panel_btn_keyword")
    async def by_keyword(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(KeywordInputModal())

    @ui.button(label="按用户搜索", style=discord.ButtonStyle.primary, emoji="👤", custom_id="search_panel_btn_user")
    async def by_user(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message(
            "请选择你要查找的用户来捉：",
            view=UserSelectView(), ephemeral=True
        )