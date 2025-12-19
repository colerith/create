import discord
from discord import app_commands, ui
from discord.ext import commands, tasks
from datetime import datetime, time
import asyncio
from zoneinfo import ZoneInfo
from utils import chimidan_text

# === 配置区域 ===
# 需要刷新日报的频道ID列表
TARGET_CHANNEL_IDS = [1450863242179121162, 1450863444373798922, 1451245427444814047]
# 允许强制刷新搜索/日报面板的管理员 ID
ADMIN_USER_ID = 1353777207042113576
# 时区设置
TZ_SHANGHAI = ZoneInfo("Asia/Shanghai")

# ==========================================
# Part 1. 通用分页视图 (用于搜索结果 & 日报)
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
            embed.add_field(
                name=f"📄 {thread.name}",
                value=f"👤 作者: {author_name}\n📂 分区: {category_name}\n🔗 [点击跳转]({thread.jump_url})",
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
# Part 2. 搜索逻辑
# ==========================================

async def execute_search(interaction: discord.Interaction, search_type: str, query_data, selected_channels):
    await interaction.response.send_message(
        chimidan_text("收到指令惹！正在全速启动搜索引擎... (0%)"), 
        ephemeral=True
    )
    target_forums = selected_channels if selected_channels else interaction.guild.forums
    
    all_threads = []
    for forum in target_forums:
        if isinstance(forum, discord.ForumChannel):
            all_threads.extend(forum.threads)

    total_count = len(all_threads)
    if total_count == 0:
        return await interaction.edit_original_response(content=chimidan_text("呜呜，当前范围内没有帖子可以搜捏..."))

    sem = asyncio.Semaphore(8) 
    results = []
    processed_count = 0

    async def check_thread(thread):
        async with sem:
            try:
                if search_type == "user":
                    if thread.owner_id == query_data.id:
                        return thread
                elif search_type == "keyword":
                    keyword = query_data.lower()
                    if keyword in thread.name.lower():
                        return thread
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

    paginator = PaginatorView(results, title=f"🔍 搜索结果: {len(results)}条", is_daily=False)
    await interaction.edit_original_response(
        content=chimidan_text(f"搜索完成惹！找到以下内容："),
        embed=paginator.get_embed(),
        view=paginator
    )


# ==========================================
# Part 3. 搜索 UI 组件
# ==========================================

class ChannelFilterView(ui.View):
    def __init__(self, search_type: str, query_data):
        super().__init__(timeout=None)
        self.search_type = search_type
        self.query_data = query_data
        self.channel_select = ui.ChannelSelect(
            placeholder="[可选] 选择特定的论坛分区...",
            channel_types=[discord.ChannelType.forum],
            min_values=0, max_values=25, row=0
        )
        self.add_item(self.channel_select)

    @ui.button(label="开始搜索", style=discord.ButtonStyle.primary, row=1, emoji="🔎")
    async def confirm_search(self, interaction: discord.Interaction, button: ui.Button):
        await execute_search(interaction, self.search_type, self.query_data, self.channel_select.values)

class KeywordInputModal(ui.Modal, title="关键词搜索"):
    keyword = ui.TextInput(label="关键词", placeholder="请输入帖子标题或内容关键词...", min_length=1)
    async def on_submit(self, interaction: discord.Interaction):
        view = ChannelFilterView(search_type="keyword", query_data=self.keyword.value)
        await interaction.response.send_message(
            chimidan_text(f"关键词“{self.keyword.value}”记录下来惹！最后一步，选个分区吧（不选就是搜全部唷）！"), 
            view=view, ephemeral=True
        )

class UserSelectView(ui.View):
    def __init__(self): super().__init__(timeout=None)
    @ui.select(cls=ui.UserSelect, placeholder="选择帖子的作者...", min_values=1, max_values=1)
    async def select_user(self, interaction: discord.Interaction, select: ui.UserSelect):
        view = ChannelFilterView(search_type="user", query_data=select.values[0])
        await interaction.response.send_message(
            chimidan_text(f"原来是找 {select.values[0].display_name} 嘟帖子... 最后一步，选个分区吧（不选就是搜全部唷）！"), 
            view=view, ephemeral=True
        )

class SearchMethodView(ui.View):
    def __init__(self):
        # 这里的 timeout=None 很重要，配合 add_view 实现持久化
        super().__init__(timeout=None) 

    # 注意：Custom ID 必须唯一且固定
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
# Part 4. Cog 主体
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

    # --- 核心逻辑调整：增加 resend 参数 ---
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

        # 如果是强制重发模式，且找到了旧消息，先删除
        if resend and target_msg:
            try: 
                await target_msg.delete()
                target_msg = None # 标记为 None，以便下面发送新的
                await asyncio.sleep(0.5)
            except: pass

        if target_msg:
            # 只有在非重发模式，且找到了旧消息时，才编辑
            try: await target_msg.edit(embed=embed, view=view)
            except: await channel.send(embed=embed, view=view)
        else:
            # 没找到旧消息，或者旧消息刚被删了，发送新的
            await channel.send(embed=embed, view=view)

    @tasks.loop(minutes=10)
    async def daily_task(self):
        for channel_id in TARGET_CHANNEL_IDS:
            channel = self.bot.get_channel(channel_id)
            if channel: 
                # 定时任务：不重发，只编辑
                await self.refresh_channel_daily_panel(channel, resend=False)

    @daily_task.before_loop
    async def before_daily_task(self):
        await self.bot.wait_until_ready()

    # --- 新增命令：手动刷新日报 (包含清理逻辑) ---
    @app_commands.command(name="更新日报", description="[管理员] 强制刷新并重发本频道的日报面板")
    async def manual_daily_report(self, interaction: discord.Interaction):
        # 鉴权
        if interaction.user.id != ADMIN_USER_ID and not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message(chimidan_text("你没有权限操作这个命令捏！"), ephemeral=True)
        
        await interaction.response.defer(ephemeral=True)
        
        if interaction.channel_id in TARGET_CHANNEL_IDS:
            # 目标频道：执行强制重发 (resend=True)
            await self.refresh_channel_daily_panel(interaction.channel, resend=True)
            await interaction.followup.send(chimidan_text("日报面板已清理并发送最新版惹！"), ephemeral=True)
        else:
            # 非目标频道：发送预览
            threads = await self.get_todays_threads(interaction.guild)
            date_str = datetime.now(TZ_SHANGHAI).strftime('%Y-%m-%d')
            view = PaginatorView(threads, title=f"📅 {date_str} 日报 (预览)", is_daily=True)
            await interaction.followup.send(embed=view.get_embed(), view=view, ephemeral=True)

    # --- 命令：更新搜索面板 (保持不变) ---
    @app_commands.command(name="更新搜索面板", description="[管理员] 清理旧面板并发送新的持久化搜索面板")
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

    # --- 临时搜索命令 ---
    @app_commands.command(name="搜索", description="调出临时搜索面板")
    async def search_cmd(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🔍 奇米蛋搜索雷达快捷版",
            description=chimidan_text("点击下方按钮开始搜索！"), 
            color=0x87ceeb
        )
        await interaction.response.send_message(embed=embed, view=SearchMethodView(), ephemeral=True)

async def setup(bot):
    await bot.add_cog(ExplorationCog(bot))