# cogs/exploration/views.py

import discord
from discord import ui
from datetime import datetime
import asyncio
import math

from config import TZ_SHANGHAI

# ==========================================
# Part 1. 核心搜索执行逻辑
# ==========================================

async def execute_search(interaction: discord.Interaction, search_type: str, query_data, selected_channels, selected_tag_ids=None):
    # 立即响应，避免超时
    await interaction.response.send_message(
        "🔍 正在全速检索中...",
        ephemeral=True
    )

    # 1. 确定搜索范围
    target_forums = []
    if selected_channels:
        for ch in selected_channels:
            full_channel = interaction.guild.get_channel(ch.id)
            if full_channel and isinstance(full_channel, discord.ForumChannel):
                target_forums.append(full_channel)
    else:
        # 默认搜索所有论坛
        target_forums = [ch for ch in interaction.guild.forums if isinstance(ch, discord.ForumChannel)]

    # 2. 收集所有帖子
    all_threads = [t for forum in target_forums for t in forum.threads]

    if not all_threads:
        return await interaction.edit_original_response(content="呜呜，当前范围内没有帖子可以搜捏...")

    # 3. 异步并发过滤
    sem = asyncio.Semaphore(10) # 稍微提高并发
    results = []
    processed_count = 0
    total_count = len(all_threads)
    target_tags_set = set(map(int, selected_tag_ids)) if selected_tag_ids else set()

    async def check_thread(thread):
        nonlocal processed_count
        try:
            async with sem:
                # 标签筛选
                if target_tags_set:
                    thread_tags = {tag.id for tag in thread.applied_tags}
                    if not (thread_tags & target_tags_set):
                        return None

                # 核心匹配逻辑
                if search_type == "user":
                    if thread.owner_id == query_data.id:
                        return thread
                elif search_type == "keyword":
                    keyword = query_data.lower()
                    # 匹配标题
                    if keyword in thread.name.lower():
                        return thread
                    # 匹配首楼内容 (需获取消息，较慢)
                    try:
                        starter = thread.starter_message
                        if not starter:
                            # 尝试获取历史第一条
                            history = [m async for m in thread.history(limit=1, oldest_first=True)]
                            if history:
                                starter = history[0]

                        if starter and starter.content and keyword in starter.content.lower():
                            return thread
                    except Exception:
                        pass
        except Exception:
            return None
        finally:
            processed_count += 1
        return None

    tasks_list = [check_thread(t) for t in all_threads]

    for future in asyncio.as_completed(tasks_list):
        result = await future
        if result:
            results.append(result)

    # 4. 结果展示
    if not results:
        return await interaction.edit_original_response(content=f"呜呜，翻遍了 {total_count} 个帖子也没找到符合条件的内容捏...")

    # 按时间倒序排列
    results.sort(key=lambda t: t.created_at or datetime.min, reverse=True)

    # 构建 Container 视图
    extra_info = f" (含标签筛选)" if selected_tag_ids else ""
    title = f"🔍 搜索结果: {len(results)}条{extra_info}"

    view = SearchResultContainer(results, title, interaction.user)
    await interaction.edit_original_response(content="", view=view)


# ==========================================
# Part 2. UI 组件 (Container 化)
# ==========================================

class SearchResultContainer(ui.LayoutView):
    """
    通用搜索结果/日报容器
    支持分页显示帖子列表
    """
    def __init__(self, data_list, title, user, is_daily=False):
        super().__init__(timeout=None if is_daily else 300) # 日报永久有效，搜索结果有时效
        self.data_list = data_list
        self.title = title
        self.user = user
        self.is_daily = is_daily

        self.per_page = 5 # Container 每个 Section 比较大，建议一页 5 个
        self.current_page = 0
        self.total_pages = math.ceil(len(data_list) / self.per_page) if data_list else 1

        # --- 控制按钮 ---
        self.btn_prev = ui.Button(emoji="⬅️", style=discord.ButtonStyle.secondary, custom_id=f"page_prev_{id(self)}")
        self.btn_prev.callback = self.on_prev

        self.btn_next = ui.Button(emoji="➡️", style=discord.ButtonStyle.secondary, custom_id=f"page_next_{id(self)}")
        self.btn_next.callback = self.on_next

        self.btn_indicator = ui.Button(label=f"1/{self.total_pages}", disabled=True, style=discord.ButtonStyle.secondary)

        # 初始化构建
        self.update_container()

    def update_container(self):
        self.clear_items()

        # 1. 顶部状态栏
        timestamp = datetime.now(TZ_SHANGHAI).strftime('%H:%M')
        header_desc = "今天好安静唷，还没有新帖子捏... 🈚️" if not self.data_list and self.is_daily else self.title
        if self.is_daily and self.data_list:
             header_desc = f"📅 **今日新帖**: 已在全服发现 {len(self.data_list)} 个新话题！"

        # 使用一个图标作为 header accessory
        icon_url = "https://cdn.discordapp.com/embed/avatars/0.png"
        if self.user:
            icon_url = self.user.display_avatar.url

        header_section = ui.Section(
            ui.TextDisplay(content=f"### {header_desc}"),
            ui.TextDisplay(content=f"-# 最后更新: {timestamp}"),
            accessory=ui.Thumbnail(media=icon_url)
        )

        elements = [header_section, ui.Separator()]

        # 2. 帖子列表 (当前页)
        start = self.current_page * self.per_page
        end = start + self.per_page
        current_items = self.data_list[start:end]

        if not current_items:
             # 空状态
             elements.append(ui.Section(
                 ui.TextDisplay(content="*暂无数据*"),
                 accessory=ui.Button(label="Waiting", disabled=True)
             ))
        else:
            for thread in current_items:
                author = thread.owner
                author_name = author.display_name if author else "未知作者"
                author_avatar = author.display_avatar.url if author else None

                # 处理标签
                tags_str = ""
                if thread.applied_tags:
                    tags = [t.name for t in thread.applied_tags[:3]]
                    tags_str = "🏷️ " + " ".join(tags)

                category_name = thread.parent.name if thread.parent else "未知分区"

                # 每个帖子一个 Section
                # Accessory 放跳转按钮
                jump_btn = ui.Button(label="传送", url=thread.jump_url, style=discord.ButtonStyle.link)

                section_content = [
                    ui.TextDisplay(content=f"**{thread.name}**"),
                    ui.TextDisplay(content=f"👤 {author_name} · 📂 {category_name}"),
                ]
                if tags_str:
                    section_content.append(ui.TextDisplay(content=f"-# {tags_str}"))

                elements.append(ui.Section(
                    *section_content,
                    accessory=jump_btn
                ))

        # 3. 底部导航栏 (ActionRow)
        # 更新按钮状态
        self.btn_prev.disabled = (self.current_page == 0)
        self.btn_next.disabled = (self.current_page >= self.total_pages - 1)
        self.btn_indicator.label = f"{self.current_page + 1} / {self.total_pages}"

        # 只有在多页时才显示翻页按钮
        action_rows = []
        if self.total_pages > 1:
            action_rows.append(ui.ActionRow(self.btn_prev, self.btn_indicator, self.btn_next))

        # 4. 组装 Container
        container = ui.Container(
            *elements,
            *action_rows,
            accent_colour=discord.Color.from_rgb(135, 206, 235) if not self.is_daily else discord.Color.gold()
        )
        self.add_item(container)

    async def on_prev(self, interaction: discord.Interaction):
        if self.current_page > 0:
            self.current_page -= 1
            self.update_container()
            await interaction.response.edit_message(view=self)

    async def on_next(self, interaction: discord.Interaction):
        if self.current_page < self.total_pages - 1:
            self.current_page += 1
            self.update_container()
            await interaction.response.edit_message(view=self)


class SearchPanelContainer(ui.LayoutView):
    """
    永久搜索面板 (Container版)
    """
    def __init__(self, bot: discord.Client): 
        super().__init__(timeout=None)
        self.bot = bot

        # 定义按钮
        self.btn_keyword = ui.Button(
            label="关键词搜索",
            style=discord.ButtonStyle.success,
            emoji="📝",
            custom_id="search_panel_btn_keyword_v2"
        )
        self.btn_keyword.callback = self.on_keyword

        self.btn_user = ui.Button(
            label="按用户搜索",
            style=discord.ButtonStyle.primary,
            emoji="👤",
            custom_id="search_panel_btn_user_v2"
        )
        self.btn_user.callback = self.on_user

        deco_img = self.bot.user.display_avatar.url

        container = ui.Container(
            ui.Section(
                ui.TextDisplay(content="### 🔍 奇米蛋搜索雷达"),
                ui.TextDisplay(content="欢迎使用全服务器资源检索系统。"),
                ui.TextDisplay(content="-# 支持跨频道搜索、标签筛选与用户定位。"),
                accessory=ui.Thumbnail(media=deco_img)
            ),
            ui.Separator(),
            ui.ActionRow(self.btn_keyword, self.btn_user),
            accent_colour=discord.Color.from_rgb(100, 149, 237) # Cornflower Blue
        )
        self.add_item(container)

    async def on_keyword(self, interaction: discord.Interaction):
        await interaction.response.send_modal(KeywordInputModal())

    async def on_user(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "请选择你要查找的用户：",
            view=UserSelectView(),
            ephemeral=True
        )


class ChannelFilterView(ui.View):
    """
    频道与标签筛选视图 (保持原 ui.View 逻辑，因为需要动态交互 Select)
    Container 目前主要用于展示，复杂的表单交互用 View + Select/Modal 依然更灵活
    """
    def __init__(self, search_type: str, query_data):
        super().__init__(timeout=180)
        self.search_type = search_type
        self.query_data = query_data

        self.channel_select = ui.ChannelSelect(
            placeholder="[可选] 选择特定的论坛分区...",
            channel_types=[discord.ChannelType.forum],
            min_values=0,
            max_values=25,
            row=0
        )
        self.channel_select.callback = self.on_channel_select
        self.add_item(self.channel_select)

    async def on_channel_select(self, interaction: discord.Interaction):
        # 移除旧的标签选择器
        for item in self.children[:]:
            if isinstance(item, TagSelect):
                self.remove_item(item)

        # 尝试获取选中频道的标签
        # 注意：ChannelSelect 返回的是 AppCommandChannel，可能只有部分信息
        selected_channels = self.channel_select.values
        if len(selected_channels) == 1:
            try:
                channel = interaction.guild.get_channel(selected_channels[0].id)
                if isinstance(channel, discord.ForumChannel) and channel.available_tags:
                    self.add_item(TagSelect(channel.available_tags))
            except:
                pass

        await interaction.response.edit_message(view=self)

    @ui.button(label="开始搜索", style=discord.ButtonStyle.primary, row=2, emoji="🔎")
    async def confirm_search(self, interaction: discord.Interaction, button: ui.Button):
        selected_tag_ids = []
        for item in self.children:
            if isinstance(item, TagSelect):
                selected_tag_ids = item.values
                break

        # 触发核心搜索逻辑
        await execute_search(
            interaction,
            self.search_type,
            self.query_data,
            self.channel_select.values,
            selected_tag_ids
        )


class TagSelect(ui.Select):
    """动态生成的标签选择器"""
    def __init__(self, tags):
        # 限制标签数量防止报错
        options = [discord.SelectOption(label=tag.name, value=str(tag.id), emoji=tag.emoji) for tag in tags[:25]]
        super().__init__(
            placeholder="[可选] 进一步筛选标签 (多选)",
            min_values=0,
            max_values=len(options),
            options=options,
            row=1
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()


class KeywordInputModal(ui.Modal, title="关键词搜索"):
    keyword = ui.TextInput(label="关键词", placeholder="请输入帖子标题或内容...", min_length=1)

    async def on_submit(self, interaction: discord.Interaction):
        view = ChannelFilterView(search_type="keyword", query_data=self.keyword.value)
        await interaction.response.send_message(
            f"🔍 关键词 **“{self.keyword.value}”** 已记录。\n请配置搜索范围（留空则搜索全站）：",
            view=view,
            ephemeral=True
        )


class UserSelectView(ui.View):
    def __init__(self):
        super().__init__(timeout=180)

    @ui.select(cls=ui.UserSelect, placeholder="选择帖子的作者...", min_values=1, max_values=1)
    async def select_user(self, interaction: discord.Interaction, select: ui.UserSelect):
        target_user = select.values[0]
        view = ChannelFilterView(search_type="user", query_data=target_user)
        await interaction.response.send_message(
            f"👤 目标用户: **{target_user.display_name}**\n请配置搜索范围：",
            view=view,
            ephemeral=True
        )