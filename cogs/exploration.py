import discord
from discord import app_commands, ui
from discord.ext import commands, tasks
from datetime import datetime, time
import asyncio
from zoneinfo import ZoneInfo
try:
    from utils import chimidan_text
except ImportError:
    def chimidan_text(text): return text

# === 配置区域 ===
TARGET_CHANNEL_IDS = [1450863242179121162, 1450863444373798922, 1451245427444814047]
ADMIN_USER_ID = 1353777207042113576
TZ_SHANGHAI = ZoneInfo("Asia/Shanghai")

# ==========================================
# Part 1. 通用分页视图
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
            # 显示标签
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
# Part 2. 搜索逻辑 (修复版)
# ==========================================

async def execute_search(interaction: discord.Interaction, search_type: str, query_data, selected_channels, selected_tag_ids=None):
    await interaction.response.send_message(
        chimidan_text("收到指令惹！正在全速启动搜索引擎... (0%)"), 
        ephemeral=True
    )
    
    # === 【关键修复开始】 ===
    # 确定搜索范围
    target_forums = []
    
    if selected_channels:
        # 如果用户选了频道，必须通过 ID 从 guild 重新获取“完整”的频道对象
        # 因为 ChannelSelect 返回的对象可能不包含 .threads 缓存数据
        for ch in selected_channels:
            full_channel = interaction.guild.get_channel(ch.id)
            if full_channel and isinstance(full_channel, discord.ForumChannel):
                target_forums.append(full_channel)
    else:
        # 如果没选，使用全服所有论坛频道
        target_forums = [ch for ch in interaction.guild.forums if isinstance(ch, discord.ForumChannel)]
    # === 【关键修复结束】 ===

    # 收集所有帖子
    all_threads = []
    for forum in target_forums:
        # 这里不需要再判断 isinstance 了，因为上面已经筛选过了
        # 注意：forum.threads 只能获取“活跃”的帖子，归档(Archived)的帖子通常获取不到
        # 如果需要搜归档帖，需要额外的异步 API 请求，会非常慢，通常只搜缓存里的活跃帖
        all_threads.extend(forum.threads)

    total_count = len(all_threads)
    if total_count == 0:
        return await interaction.edit_original_response(content=chimidan_text("呜呜，当前范围内没有帖子可以搜捏... (可能是帖子被归档了，或者机器人没加载到)"))

    sem = asyncio.Semaphore(8) 
    results = []
    processed_count = 0

    # 将 selected_tag_ids 转为集合方便计算
    target_tags_set = set(map(int, selected_tag_ids)) if selected_tag_ids else set()

    async def check_thread(thread):
        async with sem:
            try:
                # 1. 标签筛选 (如果选了标签，必须包含其中至少一个)
                if target_tags_set:
                    thread_tag_ids = {tag.id for tag in thread.applied_tags}
                    # 交集为空，说明该帖子不包含选中的任何标签，跳过
                    if not (target_tags_set & thread_tag_ids):
                        return None

                # 2. 核心搜索条件
                if search_type == "user":
                    if thread.owner_id == query_data.id:
                        return thread
                elif search_type == "keyword":
                    keyword = query_data.lower()
                    if keyword in thread.name.lower():
                        return thread
                    
                    # 只有当标题不匹配时，才去翻历史消息（减少API消耗）
                    starter = thread.starter_message
                    if not starter:
                        async for m in thread.history(limit=1, oldest_first=True):
                            starter = m; break
                    if starter and starter.content and keyword in starter.content.lower():
                        return thread
            except: pass
            return None

    tasks_list = [check_thread(t) for t in all_threads]
    last_update_time = datetime.now()

    for future in asyncio.as_completed(tasks_list):
        result = await future
        if result: results.append(result)
        processed_count += 1
        
        now = datetime.now()
        # 更新进度条 (防止频率限制，每1.5秒或完成时更新)
        if (now - last_update_time).total_seconds() > 1.5 or processed_count == total_count:
            percent = int((processed_count / total_count) * 100)
            try:
                await interaction.edit_original_response(
                    content=chimidan_text(f"正在全速搜索中... 咻咻咻！\n进度：{percent}% ({processed_count}/{total_count})\n已找到：{len(results)} 个匹配")
                )
                last_update_time = now
            except: pass

    if not results:
        return await interaction.edit_original_response(content=chimidan_text(f"呜呜，翻遍了 {total_count} 个帖子也没找到捏..."))

    # 生成结果标题
    extra_info = ""
    if selected_tag_ids:
        extra_info = f" (含标签筛选)"
    
    paginator = PaginatorView(results, title=f"🔍 搜索结果: {len(results)}条{extra_info}", is_daily=False)
    await interaction.edit_original_response(
        content=chimidan_text(f"搜索完成惹！找到以下内容："),
        embed=paginator.get_embed(),
        view=paginator
    )


# ==========================================
# Part 3. 搜索 UI 组件 (更新：动态标签选择)
# ==========================================

class TagSelect(ui.Select):
    def __init__(self, tags):
        options = []
        # 下拉菜单最多25个选项
        for tag in tags[:25]:
            emoji = tag.emoji if tag.emoji else "🏷️"
            options.append(discord.SelectOption(label=tag.name, value=str(tag.id), emoji=emoji))
        
        super().__init__(
            placeholder="[可选] 进一步筛选标签 (多选)",
            min_values=0,
            max_values=len(options),
            options=options,
            row=1 # 放在第二行
        )

    async def callback(self, interaction: discord.Interaction):
        # 仅仅为了响应交互，不需要做额外逻辑，值会存在 self.values 中
        await interaction.response.defer()

class ChannelFilterView(ui.View):
    def __init__(self, search_type: str, query_data):
        super().__init__(timeout=None)
        self.search_type = search_type
        self.query_data = query_data
        self.selected_tags = [] # 存储选中的标签ID
        
        self.channel_select = ui.ChannelSelect(
            placeholder="[可选] 选择特定的论坛分区...",
            channel_types=[discord.ChannelType.forum],
            min_values=0, max_values=25, row=0
        )
        self.channel_select.callback = self.on_channel_select # 绑定回调
        self.add_item(self.channel_select)

    async def on_channel_select(self, interaction: discord.Interaction):
        # 1. 获取当前选中的频道
        selected_channels = self.channel_select.values
        
        # 2. 清理旧的标签选择器 (如果在 View 中)
        # 我们遍历 items，移除所有类型为 TagSelect 的组件
        for item in self.children[:]:
            if isinstance(item, TagSelect):
                self.remove_item(item)

        # 3. 判断是否需要添加标签选择器
        # 逻辑：当且仅当选中了【1个】频道，且该频道是论坛时，显示标签
        if len(selected_channels) == 1:
            # channel_select.values 返回的是 AppCommandChannel 或 GuildChannel
            # 为了保险获取 available_tags，我们尝试通过 ID 从 guild 获取完整对象
            channel_id = selected_channels[0].id
            channel = interaction.guild.get_channel(channel_id)
            
            if isinstance(channel, discord.ForumChannel) and channel.available_tags:
                # 动态添加标签选择器
                tag_select = TagSelect(channel.available_tags)
                self.add_item(tag_select)
        
        # 4. 更新消息
        await interaction.response.edit_message(view=self)

    @ui.button(label="开始搜索", style=discord.ButtonStyle.primary, row=2, emoji="🔎")
    async def confirm_search(self, interaction: discord.Interaction, button: ui.Button):
        # 获取选中的标签
        selected_tag_ids = []
        for item in self.children:
            if isinstance(item, TagSelect):
                selected_tag_ids = item.values
        
        await execute_search(
            interaction, 
            self.search_type, 
            self.query_data, 
            self.channel_select.values,
            selected_tag_ids
        )

# --- 以下组件保持不变 ---

class KeywordInputModal(ui.Modal, title="关键词搜索"):
    keyword = ui.TextInput(label="关键词", placeholder="请输入帖子标题或内容关键词...", min_length=1)
    async def on_submit(self, interaction: discord.Interaction):
        view = ChannelFilterView(search_type="keyword", query_data=self.keyword.value)
        await interaction.response.send_message(
            chimidan_text(f"关键词“{self.keyword.value}”记录下来惹！\n请选择搜索范围（若只选一个分区，还可以筛选标签哦）："), 
            view=view, ephemeral=True
        )

class UserSelectView(ui.View):
    def __init__(self): super().__init__(timeout=None)
    @ui.select(cls=ui.UserSelect, placeholder="选择帖子的作者...", min_values=1, max_values=1)
    async def select_user(self, interaction: discord.Interaction, select: ui.UserSelect):
        view = ChannelFilterView(search_type="user", query_data=select.values[0])
        await interaction.response.send_message(
            chimidan_text(f"原来是找 {select.values[0].display_name} 嘟帖子...\n请选择搜索范围（若只选一个分区，还可以筛选标签哦）："), 
            view=view, ephemeral=True
        )

class SearchMethodView(ui.View):
    def __init__(self):
        super().__init__(timeout=None) 

    @ui.button(label="按关键词搜索", style=discord.ButtonStyle.success, emoji="📝", custom_id="search_panel_btn_keyword")
    async def by_keyword(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(KeywordInputModal())

    @ui.button(label="按用户搜索", style=discord.ButtonStyle.primary, emoji="👤", custom_id="search_panel_btn_user")
    async def by_user(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message(
            chimidan_text("请选择你要查找的用户来捉："), 
            view=UserSelectView(), ephemeral=True
        )

# ==========================================
# Part 4. Cog 主体 (保持不变)
# ==========================================

class ExplorationCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.bot.add_view(SearchMethodView())
        self.daily_task.start()
    
    async def cog_unload(self):
        self.daily_task.cancel()

    async def get_todays_threads(self, guild):
        today_start = datetime.now(TZ_SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        threads_list = []
        for forum in guild.forums:
            perms = forum.permissions_for(guild.me)
            if not perms.read_messages: continue
            for thread in forum.threads:
                if thread.created_at.timestamp() >= today_start:
                    threads_list.append(thread)
        threads_list.sort(key=lambda t: t.created_at.timestamp(), reverse=True)
        return threads_list

    async def refresh_channel_daily_panel(self, channel, resend=False):
        threads = await self.get_todays_threads(channel.guild)
        date_str = datetime.now(TZ_SHANGHAI).strftime('%Y年%m月%d日')
        panel_title = f"📅 {date_str} 更新日报"
        view = PaginatorView(threads, title=panel_title, is_daily=True)
        embed = view.get_embed()

        target_msg = None
        try:
            async for msg in channel.history(limit=20):
                if msg.author == self.bot.user and msg.embeds:
                    if msg.embeds[0].title and "更新日报" in msg.embeds[0].title:
                        target_msg = msg
                        break
        except Exception as e: print(f"Error scanning channel {channel.id}: {e}")

        if resend and target_msg:
            try: 
                await target_msg.delete()
                target_msg = None 
                await asyncio.sleep(0.5)
            except: pass

        if target_msg:
            try: await target_msg.edit(embed=embed, view=view)
            except: await channel.send(embed=embed, view=view)
        else:
            await channel.send(embed=embed, view=view)

    @tasks.loop(minutes=10)
    async def daily_task(self):
        for channel_id in TARGET_CHANNEL_IDS:
            channel = self.bot.get_channel(channel_id)
            if channel: 
                await self.refresh_channel_daily_panel(channel, resend=False)

    @daily_task.before_loop
    async def before_daily_task(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="更新日报面板", description="[管理] 强制刷新并重发本频道的日报面板")
    async def manual_daily_report(self, interaction: discord.Interaction):
        if interaction.user.id != ADMIN_USER_ID and not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message(chimidan_text("你没有权限操作这个命令捏！"), ephemeral=True)
        
        await interaction.response.defer(ephemeral=True)
        
        if interaction.channel_id in TARGET_CHANNEL_IDS:
            await self.refresh_channel_daily_panel(interaction.channel, resend=True)
            await interaction.followup.send(chimidan_text("日报面板已清理并发送最新版惹！"), ephemeral=True)
        else:
            threads = await self.get_todays_threads(interaction.guild)
            date_str = datetime.now(TZ_SHANGHAI).strftime('%Y-%m-%d')
            view = PaginatorView(threads, title=f"📅 {date_str} 日报 (预览)", is_daily=True)
            await interaction.followup.send(embed=view.get_embed(), view=view, ephemeral=True)

    @app_commands.command(name="更新搜索面板", description="[管理] 清理旧面板并发送新的搜索面板")
    @app_commands.default_permissions(view_audit_log=True)
    async def refresh_search_panel(self, interaction: discord.Interaction):
        if interaction.user.id != ADMIN_USER_ID:
            return await interaction.response.send_message(chimidan_text("你没有权限操作这个命令捏！"), ephemeral=True)
        
        await interaction.response.defer(ephemeral=True)
        channel = interaction.channel
        
        deleted_count = 0
        try:
            async for msg in channel.history(limit=50):
                if msg.author == self.bot.user and msg.embeds:
                    if msg.embeds[0].title == "🔍 奇米蛋搜索雷达":
                        await msg.delete()
                        deleted_count += 1
                        await asyncio.sleep(0.5)
        except Exception as e:
            print(f"Cleanup failed: {e}")

        embed = discord.Embed(
            title="🔍 奇米蛋搜索雷达",
            description=chimidan_text(
                "欢迎使用全服务器帖子搜索功能来捉！\n"
                "\n"
                "**使用指南：**\n"
                "**1️⃣ 选择模式：**点击下方按钮，选择按【关键词】还是【用户】搜索。\n"
                "**2️⃣ 输入条件：**输入你要找嘟内容，或者在列表里选人。\n"
                "**3️⃣ 筛选分区：**(可选) 指定在哪个论坛分区里找，不选就素地毯式搜索捏！\n"
                "**✨ 新功能：** 如果只选择 **一个** 论坛分区，还可以进一步筛选该分区的标签哦！\n"
                "\n"
                "点击下方按钮开始吧！"
            ), 
            color=0x87ceeb
        )
        embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        embed.set_footer(text="此面板永久有效，点击下方按钮即可使用")
        
        await channel.send(embed=embed, view=SearchMethodView())
        
        await interaction.followup.send(
            chimidan_text(f"处理完成！清理了 {deleted_count} 个旧面板，并发送了最新的搜索雷达！"), 
            ephemeral=True
        )

    @app_commands.command(name="快捷搜索", description="调出快捷搜索面板")
    async def search_cmd(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🔍 奇米蛋搜索雷达快捷版",
            description=chimidan_text("点击下方按钮开始搜索！"), 
            color=0x87ceeb
        )
        await interaction.response.send_message(embed=embed, view=SearchMethodView(), ephemeral=True)

async def setup(bot):
    await bot.add_cog(ExplorationCog(bot))
