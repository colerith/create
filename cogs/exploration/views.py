# cogs/exploration/views.py

import discord
from discord import ui
from datetime import datetime
import asyncio
import math

from config import RECOMMEND_TARGET_KEYWORDS, TZ_SHANGHAI

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


class DailyReportContainer(ui.LayoutView):
    """今日新帖与更新汇总合并到同一条消息。"""

    def __init__(self, threads, update_rows, title: str, user, guild: discord.Guild | None = None):
        super().__init__(timeout=None)
        self.threads = list(threads)
        self.update_rows = list(update_rows)
        self.title = title
        self.user = user
        self.guild = guild
        self.thread_page = 0
        self.update_page = 0
        self.thread_per_page = 4
        self.update_per_page = 5
        self.thread_total_pages = math.ceil(len(self.threads) / self.thread_per_page) if self.threads else 1
        self.update_total_pages = math.ceil(len(self.update_rows) / self.update_per_page) if self.update_rows else 1

        self.btn_thread_prev = ui.Button(emoji="⬅️", style=discord.ButtonStyle.secondary)
        self.btn_thread_prev.callback = self.on_thread_prev
        self.btn_thread_next = ui.Button(emoji="➡️", style=discord.ButtonStyle.secondary)
        self.btn_thread_next.callback = self.on_thread_next
        self.btn_thread_page = ui.Button(label="1/1", disabled=True, style=discord.ButtonStyle.secondary)

        self.btn_update_prev = ui.Button(emoji="⬅️", style=discord.ButtonStyle.secondary)
        self.btn_update_prev.callback = self.on_update_prev
        self.btn_update_next = ui.Button(emoji="➡️", style=discord.ButtonStyle.secondary)
        self.btn_update_next.callback = self.on_update_next
        self.btn_update_page = ui.Button(label="1/1", disabled=True, style=discord.ButtonStyle.secondary)

        self.update_container()

    @staticmethod
    def _clip(text, limit: int) -> str:
        text = "未命名" if text is None else str(text).strip()
        if not text:
            text = "未命名"
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 1)] + "…"

    def _channel_name(self, channel_id: int) -> str:
        channel = self.guild.get_channel(channel_id) if self.guild else None
        return channel.name if channel else f"频道 {channel_id}"

    def _thread_info(self, channel_id: int):
        thread = self.guild.get_thread(channel_id) if self.guild else None
        if thread is None and self.guild:
            channel = self.guild.get_channel(channel_id)
            thread = channel if isinstance(channel, discord.Thread) else None
        if not thread:
            return None
        parent = thread.parent
        tags = [tag.name for tag in getattr(thread, "applied_tags", [])[:4]]
        return {
            "name": thread.name,
            "forum": parent.name if parent else "未知分区",
            "tags": " ".join(tags) if tags else "无标签",
        }

    def _author_label(self, owner_id: int | None) -> str:
        if not owner_id:
            return "未知作者"
        member = self.guild.get_member(owner_id) if self.guild else None
        return f"@{member.display_name}" if member else f"@用户{owner_id}"

    def _build_threads_container(self):
        timestamp = datetime.now(TZ_SHANGHAI).strftime("%H:%M")
        icon_url = self.user.display_avatar.url if self.user else "https://cdn.discordapp.com/embed/avatars/0.png"
        header_desc = (
            f"📅 **今日新帖**: 已在全服发现 {len(self.threads)} 个新话题！"
            if self.threads
            else "今天好安静唷，还没有新帖子捏..."
        )
        elements = [
            ui.Section(
                ui.TextDisplay(content=f"### {header_desc}"),
                ui.TextDisplay(content=f"-# {self.title} · 最后更新: {timestamp}"),
                accessory=ui.Thumbnail(media=icon_url),
            ),
            ui.Separator(),
        ]

        start = self.thread_page * self.thread_per_page
        current_threads = self.threads[start : start + self.thread_per_page]
        if not current_threads:
            elements.append(ui.TextDisplay(content="*暂无新帖*"))
        else:
            for thread in current_threads:
                author = thread.owner
                author_name = author.display_name if author else "未知作者"
                tags_str = ""
                if thread.applied_tags:
                    tags_str = "🏷️ " + " ".join(t.name for t in thread.applied_tags[:3])
                category_name = thread.parent.name if thread.parent else "未知分区"
                section_content = [
                    ui.TextDisplay(content=f"**{self._clip(thread.name, 52)}**"),
                    ui.TextDisplay(content=f"👤 {author_name} · 📂 {category_name}"),
                ]
                if tags_str:
                    section_content.append(ui.TextDisplay(content=f"-# {tags_str}"))
                elements.append(
                    ui.Section(
                        *section_content,
                        accessory=ui.Button(label="传送", url=thread.jump_url, style=discord.ButtonStyle.link),
                    )
                )

        self.btn_thread_prev.disabled = self.thread_page == 0
        self.btn_thread_next.disabled = self.thread_page >= self.thread_total_pages - 1
        self.btn_thread_page.label = f"新帖 {self.thread_page + 1}/{self.thread_total_pages}"
        if self.thread_total_pages > 1:
            elements.append(ui.ActionRow(self.btn_thread_prev, self.btn_thread_page, self.btn_thread_next))
        return ui.Container(*elements, accent_colour=discord.Color.gold())

    def _build_updates_container(self):
        timestamp = datetime.now(TZ_SHANGHAI).strftime("%H:%M")
        elements = [
            ui.TextDisplay(
                content=(
                    f"### ✨ 更新汇总\n今日共记录 **{len(self.update_rows)}** 条作品更新。"
                    if self.update_rows
                    else "### ✨ 更新汇总\n今天还没有发布更新日志的作品。"
                )
            ),
            ui.TextDisplay(content=f"-# 最后更新: {timestamp}"),
            ui.Separator(),
        ]

        start = self.update_page * self.update_per_page
        current_rows = self.update_rows[start : start + self.update_per_page]
        if not current_rows:
            elements.append(ui.TextDisplay(content="*暂无更新记录*"))
        else:
            for row in current_rows:
                info = self._thread_info(row["channel_id"])
                title = self._clip((info or {}).get("name") or row["title"], 42)
                forum = self._clip((info or {}).get("forum") or self._channel_name(row["channel_id"]), 24)
                tags = self._clip((info or {}).get("tags") or "无标签", 42)
                guild_id = row["guild_id"] or (self.guild.id if self.guild else None)
                jump_url = (
                    f"https://discord.com/channels/{guild_id}/{row['channel_id']}/{row['update_message_id']}"
                    if guild_id
                    else None
                )
                jump_text = f"[跳转]({jump_url})" if jump_url else "跳转不可用"
                author = self._author_label(row["owner_id"])
                elements.append(
                    ui.TextDisplay(
                        content=(
                            f"**{title}**\n"
                            f"-# 作者: {author} · 分区: {forum}\n"
                            f"-# 标签: {tags} · {jump_text}"
                        )
                    )
                )

        self.btn_update_prev.disabled = self.update_page == 0
        self.btn_update_next.disabled = self.update_page >= self.update_total_pages - 1
        self.btn_update_page.label = f"更新 {self.update_page + 1}/{self.update_total_pages}"
        if self.update_total_pages > 1:
            elements.append(ui.ActionRow(self.btn_update_prev, self.btn_update_page, self.btn_update_next))
        return ui.Container(*elements, accent_colour=discord.Color.green())

    def update_container(self):
        self.clear_items()
        self.add_item(self._build_threads_container())
        self.add_item(self._build_updates_container())

    async def on_thread_prev(self, interaction: discord.Interaction):
        if self.thread_page > 0:
            self.thread_page -= 1
        self.update_container()
        await interaction.response.edit_message(view=self)

    async def on_thread_next(self, interaction: discord.Interaction):
        if self.thread_page < self.thread_total_pages - 1:
            self.thread_page += 1
        self.update_container()
        await interaction.response.edit_message(view=self)

    async def on_update_prev(self, interaction: discord.Interaction):
        if self.update_page > 0:
            self.update_page -= 1
        self.update_container()
        await interaction.response.edit_message(view=self)

    async def on_update_next(self, interaction: discord.Interaction):
        if self.update_page < self.update_total_pages - 1:
            self.update_page += 1
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

        self.btn_downloads = ui.Button(
            label="下载记录",
            style=discord.ButtonStyle.secondary,
            emoji="📥",
            custom_id="search_panel_btn_downloads_v1"
        )
        self.btn_downloads.callback = self.on_downloads

        self.btn_my_works = ui.Button(
            label="我的作品",
            style=discord.ButtonStyle.secondary,
            emoji="📚",
            custom_id="search_panel_btn_my_works_v1"
        )
        self.btn_my_works.callback = self.on_my_works

        deco_img = self.bot.user.display_avatar.url

        container = ui.Container(
            ui.Section(
                ui.TextDisplay(content="### 🥚 奇米蛋探索台"),
                ui.TextDisplay(content="把散落在论坛里的作品、更新和足迹都收进一只小蛋壳。"),
                ui.TextDisplay(content="-# 搜索、回访、整理自己的作品，都从这里出发。"),
                accessory=ui.Thumbnail(media=deco_img)
            ),
            ui.Separator(),
            ui.TextDisplay(
                content=(
                    "**🔎 找作品**\n"
                    "-# 用关键词翻标题和首楼内容，或直接按作者定位帖子。"
                )
            ),
            ui.ActionRow(self.btn_keyword, self.btn_user),
            ui.Separator(),
            ui.TextDisplay(
                content=(
                    "**📚 我的收藏与创作**\n"
                    "-# 下载记录会把最近更新的作品放在前面；我的作品按帖子汇总，可筛选分区并推送。"
                )
            ),
            ui.ActionRow(self.btn_downloads, self.btn_my_works),
            ui.Separator(),
            ui.TextDisplay(content="-# 今日状态：雷达待命中，轻点按钮就开始找蛋。"),
            accent_colour=discord.Color.from_rgb(255, 183, 197)
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

    async def on_downloads(self, interaction: discord.Interaction):
        cog = self.bot.get_cog("ExplorationCog")
        if not cog:
            return await interaction.response.send_message("探索模块尚未就绪。", ephemeral=True)
        await cog.send_download_library_panel(interaction)

    async def on_my_works(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "请选择要筛选的作品分区，留空可查看全部。",
            view=MyWorksFilterView(self.bot),
            ephemeral=True
        )


class PagedRecordContainer(ui.LayoutView):
    """下载记录/我的作品共用的文本分页面板基类。"""

    panel_type = ""
    accent_colour = discord.Color.blurple()

    def __init__(self, rows, title: str, user, guild: discord.Guild | None, bot: discord.Client, timeout=300):
        super().__init__(timeout=timeout)
        self.rows = list(rows)
        self.title = title
        self.user = user
        self.guild = guild
        self.bot = bot
        self.per_page = 6
        self.current_page = 0
        self.total_pages = math.ceil(len(self.rows) / self.per_page) if self.rows else 1

        uid = user.id if user else id(self)
        self.btn_prev = ui.Button(emoji="⬅️", style=discord.ButtonStyle.secondary, custom_id=f"{self.panel_type}_prev_{uid}_{id(self)}")
        self.btn_prev.callback = self.on_prev
        self.btn_next = ui.Button(emoji="➡️", style=discord.ButtonStyle.secondary, custom_id=f"{self.panel_type}_next_{uid}_{id(self)}")
        self.btn_next.callback = self.on_next
        self.btn_indicator = ui.Button(label=f"1/{self.total_pages}", disabled=True, style=discord.ButtonStyle.secondary)
        self.btn_push_dm = ui.Button(label="推送到私信", emoji="📬", style=discord.ButtonStyle.primary)
        self.btn_push_dm.callback = self.on_push_dm
        self.btn_delete_dm = ui.Button(label="删除私信推送", emoji="🗑️", style=discord.ButtonStyle.danger)
        self.btn_delete_dm.callback = self.on_delete_dm

        self.update_container()

    @staticmethod
    def _clip(text, limit: int) -> str:
        text = "" if text is None else str(text).strip()
        if not text:
            text = "未命名"
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 1)] + "…"

    def _jump_url(self, channel_id: int | None, message_id: int | None) -> str | None:
        guild_id = self.guild.id if self.guild else None
        if not guild_id or not channel_id or not message_id:
            return None
        return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.user and interaction.user.id != self.user.id:
            await interaction.response.send_message("这个面板只属于发起者。", ephemeral=True)
            return False
        return True

    def _row_lines(self, current_rows):
        return ["*暂无数据*"]

    def update_container(self):
        self.clear_items()

        timestamp = datetime.now(TZ_SHANGHAI).strftime("%H:%M")
        icon_url = self.user.display_avatar.url if self.user else "https://cdn.discordapp.com/embed/avatars/0.png"
        elements = [
            ui.Section(
                ui.TextDisplay(content=f"### {self.title}\n共 **{len(self.rows)}** 条记录。"),
                ui.TextDisplay(content=f"-# 最后更新: {timestamp}"),
                accessory=ui.Thumbnail(media=icon_url),
            ),
            ui.Separator(),
        ]

        start = self.current_page * self.per_page
        current_rows = self.rows[start : start + self.per_page]
        elements.append(ui.TextDisplay(content="\n\n".join(self._row_lines(current_rows))))

        self.btn_prev.disabled = self.current_page == 0
        self.btn_next.disabled = self.current_page >= self.total_pages - 1
        self.btn_indicator.label = f"{self.current_page + 1} / {self.total_pages}"

        rows = [ui.ActionRow(self.btn_push_dm, self.btn_delete_dm)]
        if self.total_pages > 1:
            rows.append(ui.ActionRow(self.btn_prev, self.btn_indicator, self.btn_next))

        self.add_item(ui.Container(*elements, *rows, accent_colour=self.accent_colour))

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

    async def on_push_dm(self, interaction: discord.Interaction):
        cog = self.bot.get_cog("ExplorationCog")
        if not cog:
            return await interaction.response.send_message("探索模块尚未就绪。", ephemeral=True)
        await cog.push_panel_to_dm(interaction, self.panel_type, self.user.id)

    async def on_delete_dm(self, interaction: discord.Interaction):
        cog = self.bot.get_cog("ExplorationCog")
        if not cog:
            return await interaction.response.send_message("探索模块尚未就绪。", ephemeral=True)
        await cog.delete_dm_panel_push(interaction, self.panel_type, self.user.id)


class DownloadLibraryContainer(PagedRecordContainer):
    panel_type = "download_library"
    accent_colour = discord.Color.from_rgb(255, 183, 197)

    def __init__(self, rows, title: str, user, guild: discord.Guild | None, bot: discord.Client, timeout=300):
        self.summary_mode = "category"
        self.btn_mode_category = ui.Button(label="分类汇总", style=discord.ButtonStyle.primary)
        self.btn_mode_category.callback = self.on_mode_category
        self.btn_mode_month = ui.Button(label="月份汇总", style=discord.ButtonStyle.secondary)
        self.btn_mode_month.callback = self.on_mode_month
        self.btn_mode_author = ui.Button(label="作者汇总", style=discord.ButtonStyle.secondary)
        self.btn_mode_author.callback = self.on_mode_author
        self.btn_mode_latest = ui.Button(label="无汇总", style=discord.ButtonStyle.secondary)
        self.btn_mode_latest.callback = self.on_mode_latest
        super().__init__(rows, title, user, guild, bot, timeout=timeout)

    def _row_time(self, row):
        return row.get("latest_update_at") or row.get("touched_at") or row.get("created_at") or ""

    def _row_month(self, row):
        raw = self._row_time(row)
        try:
            return datetime.fromisoformat(raw).strftime("%Y年%m月")
        except Exception:
            return "时间未明"

    def _category_name(self, row):
        haystack = " ".join(
            str(row.get(key) or "")
            for key in ("title", "channel_name", "forum_name", "tags")
        )
        for keyword in RECOMMEND_TARGET_KEYWORDS:
            if keyword in haystack:
                return keyword
        return "其他作品"

    def _grouped_blocks(self):
        grouped = {}
        if self.summary_mode == "month":
            key_func = self._row_month
        elif self.summary_mode == "author":
            key_func = lambda row: row.get("author_name") or f"作者 {row.get('owner_id')}"
        else:
            key_func = self._category_name

        for row in self.rows:
            grouped.setdefault(key_func(row), []).append(row)

        if self.summary_mode == "category":
            ordered_keys = [key for key in RECOMMEND_TARGET_KEYWORDS if key in grouped]
            ordered_keys.extend(key for key in grouped if key not in ordered_keys)
        else:
            ordered_keys = list(grouped.keys())
        return [(key, grouped[key]) for key in ordered_keys]

    def _display_units(self):
        units = []
        chunk_size = 3
        if self.summary_mode == "latest":
            for start in range(0, len(self.rows), chunk_size):
                units.append(("最新足迹", self.rows[start : start + chunk_size], start, len(self.rows)))
            return units

        for group_name, group_rows in self._grouped_blocks():
            for start in range(0, len(group_rows), chunk_size):
                units.append((group_name, group_rows[start : start + chunk_size], start, len(group_rows)))
        return units

    def _item_line(self, row, idx: int | None = None):
        title = self._clip(row.get("title"), 34)
        post_name = self._clip(row.get("post_name") or row.get("channel_name") or title, 34)
        author = self._clip(row.get("author_name") or f"作者 {row.get('owner_id')}", 18)
        channel = self._clip(row.get("forum_name") or row.get("channel_name"), 20)
        tags = self._clip(row.get("tags") or "无标签", 28)
        post_url = self._jump_url(row.get("channel_id"), row.get("message_id"))
        update_url = self._jump_url(row.get("channel_id"), row.get("latest_update_message_id"))
        post_link = f"[去看看]({post_url})" if post_url else "链接暂不可用"
        update_text = "有新鲜更新" if row.get("latest_update_at") else "暂未更新"
        update_link = f" · [更新日志]({update_url})" if update_url else ""
        prefix = f"{idx}. " if idx is not None else ""
        return (
            f"{prefix}**{title}**\n"
            f"-# 作者: {author}\n"
            f"-# 原帖: {post_name}\n"
            f"-# 频道: {channel} · 标签: {tags}\n"
            f"-# {update_text} · {post_link}{update_link}"
        )

    def _mode_label(self):
        labels = {
            "category": "按奇米蛋分区小篮子收好啦",
            "month": "按月份把下载足迹叠成小册子",
            "author": "按作者摆成一排创作小摊",
            "latest": "从最新到最旧，一路顺滑翻下去",
        }
        return labels.get(self.summary_mode, labels["category"])

    def update_container(self):
        self.clear_items()
        timestamp = datetime.now(TZ_SHANGHAI).strftime("%H:%M")
        icon_url = self.user.display_avatar.url if self.user else "https://cdn.discordapp.com/embed/avatars/0.png"
        self.per_page = 2
        data_units = self._display_units()
        self.total_pages = math.ceil(len(data_units) / self.per_page) if data_units else 1
        if self.current_page >= self.total_pages:
            self.current_page = max(0, self.total_pages - 1)

        header_elements = [
            ui.Section(
                ui.TextDisplay(content=f"### 🥚 下载记录小窝\n摸摸蛋壳，今天帮你收好 **{len(self.rows)}** 个看过/关注过的作品。"),
                ui.TextDisplay(content=f"-# {self._mode_label()} · 最后更新: {timestamp}"),
                accessory=ui.Thumbnail(media=icon_url),
            ),
            ui.Separator(),
        ]
        self.add_item(ui.Container(*header_elements, accent_colour=self.accent_colour))

        if not data_units:
            self.add_item(
                ui.Container(
                    ui.TextDisplay(
                    content=(
                        "### 🌸 小篮子还是空的\n"
                        "下载、点赞或评论过的保护帖会出现在这里；有更新日志的作品会被悄悄捧到前面。"
                    )
                    ),
                    accent_colour=discord.Color.from_rgb(255, 210, 225),
                )
            )
        else:
            start = self.current_page * self.per_page
            current_units = data_units[start : start + self.per_page]
            for group_name, group_rows, group_start, group_total in current_units:
                start_no = group_start + 1
                end_no = group_start + len(group_rows)
                if self.summary_mode == "latest":
                    title = f"### 🐣 最新足迹 · {start_no}-{end_no}/{group_total}"
                    lines = [
                        self._item_line(row, idx)
                        for idx, row in enumerate(group_rows, start=group_start + 1)
                    ]
                else:
                    title = f"### ✨ {self._clip(group_name, 32)} · {start_no}-{end_no}/{group_total}"
                    lines = [self._item_line(row) for row in group_rows]
                block_content = title + "\n" + "\n\n".join(lines)
                self.add_item(
                    ui.Container(
                        ui.TextDisplay(content=block_content),
                        accent_colour=discord.Color.from_rgb(255, 224, 232),
                    )
                )

        self.btn_prev.disabled = self.current_page == 0
        self.btn_next.disabled = self.current_page >= self.total_pages - 1
        self.btn_indicator.label = f"{self.current_page + 1} / {self.total_pages}"
        for mode, button in (
            ("category", self.btn_mode_category),
            ("month", self.btn_mode_month),
            ("author", self.btn_mode_author),
            ("latest", self.btn_mode_latest),
        ):
            button.style = discord.ButtonStyle.primary if self.summary_mode == mode else discord.ButtonStyle.secondary

        action_rows = [
            ui.ActionRow(self.btn_mode_category, self.btn_mode_month, self.btn_mode_author, self.btn_mode_latest),
            ui.ActionRow(self.btn_push_dm, self.btn_delete_dm),
        ]
        if self.total_pages > 1:
            action_rows.append(ui.ActionRow(self.btn_prev, self.btn_indicator, self.btn_next))

        self.add_item(ui.Container(*action_rows, accent_colour=discord.Color.from_rgb(245, 245, 245)))

    async def _set_mode(self, interaction: discord.Interaction, mode: str):
        self.summary_mode = mode
        self.current_page = 0
        self.update_container()
        await interaction.response.edit_message(view=self)

    async def on_mode_category(self, interaction: discord.Interaction):
        await self._set_mode(interaction, "category")

    async def on_mode_month(self, interaction: discord.Interaction):
        await self._set_mode(interaction, "month")

    async def on_mode_author(self, interaction: discord.Interaction):
        await self._set_mode(interaction, "author")

    async def on_mode_latest(self, interaction: discord.Interaction):
        await self._set_mode(interaction, "latest")

    def _row_lines(self, current_rows):
        if not current_rows:
            return ["*还没有下载、点赞或评论过的保护帖。*"]
        lines = []
        for idx, row in enumerate(current_rows, start=self.current_page * self.per_page + 1):
            title = self._clip(row["title"], 64)
            post_url = self._jump_url(row["channel_id"], row["message_id"])
            update_url = self._jump_url(row["channel_id"], row["latest_update_message_id"])
            sources = self._clip(row["sources"], 24)
            updated = "最近更新" if row["latest_update_at"] else "暂无更新"
            link = f"[帖子]({post_url})" if post_url else "帖子链接不可用"
            update_link = f" | [更新日志]({update_url})" if update_url else ""
            lines.append(f"**{idx}. {title}**\n-# {updated} · 来源: {sources} · {link}{update_link}")
        return lines


class MyWorksContainer(PagedRecordContainer):
    panel_type = "my_works"
    accent_colour = discord.Color.from_rgb(82, 190, 128)

    def __init__(self, rows, title: str, user, guild: discord.Guild | None, bot: discord.Client, selected_channel_ids=None, timeout=300):
        self.selected_channel_ids = list(selected_channel_ids or [])
        self.btn_push_channel = ui.Button(label="推送到频道", emoji="📣", style=discord.ButtonStyle.success)
        self.btn_push_channel.callback = self.on_push_channel
        super().__init__(rows, title, user, guild, bot, timeout=timeout)
        self.update_container()

    def update_container(self):
        self.clear_items()

        timestamp = datetime.now(TZ_SHANGHAI).strftime("%H:%M")
        icon_url = self.user.display_avatar.url if self.user else "https://cdn.discordapp.com/embed/avatars/0.png"
        filter_text = "全部分区" if not self.selected_channel_ids else "、".join(self.selected_channel_ids)
        self.per_page = 6
        self.total_pages = math.ceil(len(self.rows) / self.per_page) if self.rows else 1
        if self.current_page >= self.total_pages:
            self.current_page = max(0, self.total_pages - 1)

        self.add_item(
            ui.Container(
                ui.Section(
                    ui.TextDisplay(content=f"### 📚 我的作品小书架\n这里按帖子汇总，共找到 **{len(self.rows)}** 个本服务器作品帖。"),
                    ui.TextDisplay(content=f"-# 筛选: {filter_text} · 最后更新: {timestamp}"),
                    accessory=ui.Thumbnail(media=icon_url),
                ),
                ui.Separator(),
                accent_colour=self.accent_colour,
            )
        )

        start = self.current_page * self.per_page
        current_rows = self.rows[start : start + self.per_page]
        if not current_rows:
            self.add_item(
                ui.Container(
                    ui.TextDisplay(content="### 🌱 这里还空着\n当前筛选下还没有找到你发布过的帖子。"),
                    accent_colour=discord.Color.from_rgb(210, 245, 225),
                )
            )
        else:
            content_items = []
            for idx, row in enumerate(current_rows, start=start + 1):
                post_name = self._clip(row.get("post_name"), 42)
                tags = self._clip(row.get("tags") or "无标签", 44)
                category = self._clip(row.get("category") or "其他", 16)
                forum = self._clip(row.get("forum_name") or "未知分区", 24)
                like_count = "--" if row.get("like_count") is None else str(int(row.get("like_count") or 0))
                comment_count = "--" if row.get("comment_count") is None else str(int(row.get("comment_count") or 0))
                attachment_count = int(row.get("attachment_count") or 0)
                jump_url = row.get("jump_url") or self._jump_url(row.get("channel_id"), row.get("latest_message_id"))
                content_items.append(
                    ui.Section(
                        ui.TextDisplay(content=f"**{idx}. {post_name}**"),
                        ui.TextDisplay(
                            content=(
                                f"-# 分区: {category} · 频道: {forum}\n"
                                f"-# 标签: {tags}\n"
                                f"-# ♡ {like_count} · 评论 {comment_count} · 附件 {attachment_count}"
                            )
                        ),
                        accessory=ui.Button(label="跳转", url=jump_url, style=discord.ButtonStyle.link)
                        if jump_url
                        else ui.Button(label="无链接", disabled=True),
                    )
                )
            self.add_item(
                ui.Container(
                    *content_items,
                    accent_colour=discord.Color.from_rgb(218, 247, 232),
                )
            )

        self.btn_prev.disabled = self.current_page == 0
        self.btn_next.disabled = self.current_page >= self.total_pages - 1
        self.btn_indicator.label = f"{self.current_page + 1} / {self.total_pages}"

        action_rows = [ui.ActionRow(self.btn_push_dm, self.btn_delete_dm, self.btn_push_channel)]
        if self.total_pages > 1:
            action_rows.append(ui.ActionRow(self.btn_prev, self.btn_indicator, self.btn_next))
        self.add_item(ui.Container(*action_rows, accent_colour=discord.Color.from_rgb(245, 245, 245)))

    async def on_push_channel(self, interaction: discord.Interaction):
        await interaction.response.send_modal(MyWorksChannelPushModal(self.bot, self.user.id, self.selected_channel_ids))

    async def on_push_dm(self, interaction: discord.Interaction):
        cog = self.bot.get_cog("ExplorationCog")
        if not cog:
            return await interaction.response.send_message("探索模块尚未就绪。", ephemeral=True)
        await cog.push_panel_to_dm(
            interaction,
            self.panel_type,
            self.user.id,
            selected_channel_ids=self.selected_channel_ids,
        )


class MyWorksFilterView(ui.View):
    def __init__(self, bot: discord.Client):
        super().__init__(timeout=180)
        self.bot = bot
        options = [discord.SelectOption(label="全部", value="__all__", description="显示全部作品帖")]
        options.extend(
            discord.SelectOption(label=keyword, value=keyword, description=f"只看 {keyword} 分区")
            for keyword in RECOMMEND_TARGET_KEYWORDS
        )
        self.category_select = ui.Select(
            placeholder="选择作品分区筛选...",
            options=options,
            min_values=0,
            max_values=len(options),
            row=0,
        )
        self.category_select.callback = self.on_category_select
        self.add_item(self.category_select)

    async def on_category_select(self, interaction: discord.Interaction):
        await interaction.response.defer()

    @ui.button(label="查看我的作品", style=discord.ButtonStyle.primary, row=1, emoji="📚")
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        cog = self.bot.get_cog("ExplorationCog")
        if not cog:
            return await interaction.response.send_message("探索模块尚未就绪。", ephemeral=True)
        selected = [value for value in self.category_select.values if value != "__all__"]
        await cog.send_my_works_panel(interaction, selected_channel_ids=selected)


class MyWorksChannelPushModal(ui.Modal, title="推送我的作品到频道"):
    channel_link = ui.TextInput(
        label="频道链接或频道ID",
        placeholder="https://discord.com/channels/服务器ID/频道ID 或直接输入频道ID",
        min_length=1,
        max_length=200,
    )

    def __init__(self, bot: discord.Client, user_id: int, selected_channel_ids=None):
        super().__init__(timeout=180)
        self.bot = bot
        self.user_id = user_id
        self.selected_channel_ids = list(selected_channel_ids or [])

    async def on_submit(self, interaction: discord.Interaction):
        cog = self.bot.get_cog("ExplorationCog")
        if not cog:
            return await interaction.response.send_message("探索模块尚未就绪。", ephemeral=True)
        await cog.push_my_works_to_channel(
            interaction,
            self.user_id,
            str(self.channel_link.value),
            selected_channel_ids=self.selected_channel_ids,
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
