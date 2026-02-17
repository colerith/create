import discord
from discord import app_commands, ui
from discord.ext import commands, tasks
import aiosqlite
import random
import asyncio

from datetime import datetime, time
from zoneinfo import ZoneInfo
from cogs.protection.db import get_db
from config import TZ_SHANGHAI, TARGET_KEYWORDS, TEST_ROLE_ID, DAILY_RECOMMEND_CHANNEL_ID


# ==========================================
# Part 1. 数据库初始化与操作
# ==========================================

async def init_recommend_db():
    """在Cog加载时检查并创建抽卡记录表"""
    async with get_db() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS daily_gacha_records (
                user_id INTEGER PRIMARY KEY,
                last_draw_date TEXT
            )
        """)
        await db.commit()

async def check_user_drawn_today(user_id: int) -> bool:
    """检查用户今天是否已经抽过卡"""
    today_str = datetime.now(TZ_SHANGHAI).strftime("%Y-%m-%d")
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT last_draw_date FROM daily_gacha_records WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        
    if row and row['last_draw_date'] == today_str:
        return True
    return False

async def mark_user_drawn(user_id: int):
    """标记用户今天已抽卡"""
    today_str = datetime.now(TZ_SHANGHAI).strftime("%Y-%m-%d")
    async with get_db() as db:
        await db.execute(
            "INSERT OR REPLACE INTO daily_gacha_records (user_id, last_draw_date) VALUES (?, ?)",
            (user_id, today_str)
        )
        await db.commit()

# ==========================================
# Part 2. 辅助函数
# ==========================================

def get_card_forums(guild: discord.Guild):
    """【修改】获取所有包含目标关键词的论坛频道"""
    # 只要频道名包含列表中的任意一个关键词，就纳入池子
    return [c for c in guild.forums if any(keyword in c.name for keyword in TARGET_KEYWORDS)]

async def get_random_thread_pool(guild: discord.Guild, specific_channel_id=None):
    """获取符合条件的帖子池 (排除置顶帖)"""
    forums = get_card_forums(guild)
    if specific_channel_id:
        forums = [f for f in forums if f.id == int(specific_channel_id)]
    
    threads = []
    for forum in forums:
        for thread in forum.threads:
            # 排除置顶帖
            if thread.flags.pinned:
                continue
            threads.append(thread)
            
    return threads

async def fetch_thread_details(thread: discord.Thread):
    """获取帖子的详细信息 (优化版)"""
    starter = thread.starter_message
    if not starter:
        try:
            async for msg in thread.history(limit=1, oldest_first=True):
                starter = msg; break
        except: pass
    
    intro = "（暂无介绍）"
    image_url = None
    
    if starter:
        # --- 简介处理逻辑 ---
        if starter.content:
            raw_text = starter.content
            lines = raw_text.split('\n')
            MAX_LINES = 8
            if len(lines) > MAX_LINES:
                display_text = "\n".join(lines[:MAX_LINES]) + "\n..."
            else:
                display_text = raw_text
                
            if len(display_text) > 300:
                display_text = display_text[:300] + "..."
                
            intro = display_text

        # --- 图片获取逻辑 ---
        if starter.attachments:
            for att in starter.attachments:
                if att.content_type and "image" in att.content_type:
                    image_url = att.url; break
    
    tags = [tag.name for tag in thread.applied_tags] if thread.applied_tags else ["无标签"]
    
    owner = thread.owner
    author_name = owner.display_name if owner else "未知作者"
    author_mention = owner.mention if owner else "未知作者"
    author_avatar = owner.display_avatar.url if owner else None

    return {
        "title": thread.name,
        "author_name": author_name,
        "author_mention": author_mention,
        "author_avatar": author_avatar,
        "intro": intro,
        "category": thread.parent.name,
        "tags": tags,
        "url": thread.jump_url,
        "image": image_url
    }

# ==========================================
# Part 3. UI 视图 (抽卡控制台)
# ==========================================

class GachaControlView(ui.View):
    def __init__(self, guild_forums):
        super().__init__(timeout=None)
        self.selected_channel_id = None
        # 下拉菜单选项：全部分区 + 各个匹配到的分区
        options = [discord.SelectOption(label="🌐 全部分区 (默认)", value="all", description="从所有资源分区随机抽取")]
        for forum in guild_forums[:24]: # 下拉菜单上限25个选项
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

    async def execute_draw(self, interaction: discord.Interaction, count: int):
        is_tester = False
        if isinstance(interaction.user, discord.Member):
            if interaction.user.get_role(TEST_ROLE_ID):
                is_tester = True
        
        if not is_tester:
            if await check_user_drawn_today(interaction.user.id):
                return await interaction.response.send_message("🔮 您今天已经感应过缘分啦，请明天再来吧！", ephemeral=True)
        
        await interaction.response.defer(ephemeral=True)
        
        threads = await get_random_thread_pool(interaction.guild, self.selected_channel_id)
        if not threads:
            return await interaction.followup.send("🏜️ 当前选择的卡池里空空如也... (或是只有置顶帖)", ephemeral=True)
            
        if len(threads) < count: count = len(threads)
            
        drawn_threads = random.sample(threads, count)
        
        if not is_tester:
            await mark_user_drawn(interaction.user.id)
        
        embeds = []
        if count == 1:
            t = drawn_threads[0]
            info = await fetch_thread_details(t)
            
            embed = discord.Embed(
                title=f"✨ 命运的邂逅：{info['title']}", 
                description=f"👤 作者: {info['author_mention']}\n\n{info['intro']}", 
                color=0xffd700, 
                url=info['url']
            )
            embed.set_author(name=info['author_name'], icon_url=info['author_avatar'])
            
            embed.add_field(name="📂 分区", value=info['category'], inline=True)
            embed.add_field(name="🏷️ 标签", value=" / ".join(info['tags']), inline=True)
            if info['image']: embed.set_image(url=info['image'])
            
            ft_text = "今日缘分已定，点击标题即可跳转！"
            if is_tester: ft_text += " (测试员模式：不消耗次数)"
            embed.set_footer(text=ft_text)
            embeds.append(embed)
        else:
            main_embed = discord.Embed(title=f"💫 恭喜获得 {count} 连抽结果！", color=0xff69b4)
            main_embed.set_footer(text=f"点击蓝色标题即可跳转详情{' (测试员模式)' if is_tester else ''}")
            embeds.append(main_embed)
            
            desc_text = ""
            for i, t in enumerate(drawn_threads):
                tags = [tag.name for tag in t.applied_tags[:3]]
                tag_str = f"[{' '.join(tags)}]" if tags else ""
                desc_text += f"{i+1}. **[{t.name}]({t.jump_url})** - {t.owner.display_name if t.owner else '未知'} {tag_str}\n"
            main_embed.description = desc_text

        await interaction.followup.send(embeds=embeds, ephemeral=True)

    @ui.button(label="单抽 (1发)", style=discord.ButtonStyle.primary, row=1, emoji="1️⃣")
    async def draw_one(self, i: discord.Interaction, b: ui.Button): await self.execute_draw(i, 1)

    @ui.button(label="五连抽 (5发)", style=discord.ButtonStyle.secondary, row=1, emoji="5️⃣")
    async def draw_five(self, i: discord.Interaction, b: ui.Button): await self.execute_draw(i, 5)

    @ui.button(label="十连抽 (10发)", style=discord.ButtonStyle.success, row=1, emoji="🔟")
    async def draw_ten(self, i: discord.Interaction, b: ui.Button): await self.execute_draw(i, 10)


class DailyRecommendView(ui.View):
    def __init__(self): super().__init__(timeout=None)

    @ui.button(label="🔮 抽取今日缘分", style=discord.ButtonStyle.primary, custom_id="daily_gacha_open_btn")
    async def open_gacha(self, interaction: discord.Interaction, button: ui.Button):
        forums = get_card_forums(interaction.guild)
        if not forums: return await interaction.response.send_message("本服务器没有配置相关资源频道，无法抽卡。", ephemeral=True)
        view = GachaControlView(forums)
        await interaction.response.send_message(
            "🎴 **抽卡控制台已启动**\n请选择想要抽取的卡池（默认全部），然后点击抽卡按钮。\n*每天仅限抽取一次哦！*", 
            view=view, ephemeral=True
        )

# ==========================================
# Part 4. Cog 主逻辑
# ==========================================

class RecommendCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.bot.add_view(DailyRecommendView())
        self.bot.loop.create_task(init_recommend_db())
        self.daily_recommend_task.start()

    async def cog_unload(self):
        self.daily_recommend_task.cancel()

    async def _cleanup_old_messages(self, channel):
        """删除旧的推荐消息"""
        try:
            async for msg in channel.history(limit=20):
                if msg.author == self.bot.user and msg.embeds:
                    # 【修改】这里只要标题包含 "每日精选" 就匹配，兼容旧的 "每日精选角色"
                    if "每日精选" in msg.embeds[0].title:
                        await msg.delete()
                        await asyncio.sleep(0.5)
        except Exception as e: print(f"Cleanup error: {e}")

    async def refresh_recommendation_panel(self, channel, mode="edit"):
        """
        核心刷新逻辑
        mode="edit":  尝试编辑已有消息，若无则发送（用于每日自动）
        mode="reset": 强制删除旧消息并发送新的（用于手动命令）
        """
        # 1. 获取数据
        pool = await get_random_thread_pool(channel.guild)
        if not pool:
            # 如果池子空了，发个提示
            error_embed = discord.Embed(title="📅 每日推荐", description="今天资源库里空空如也捏...", color=0x99aab5)
            if mode == "reset":
                await self._cleanup_old_messages(channel)
                await channel.send(embed=error_embed)
            return

        target_thread = random.choice(pool)
        info = await fetch_thread_details(target_thread)
        
        # 2. 构建 Embed
        date_str = datetime.now(TZ_SHANGHAI).strftime("%m月%d日")
        
        # 【修改】标题去掉了“角色”二字
        embed = discord.Embed(
            title=f"📅 {date_str} · 每日精选", 
            description=f"### [{info['title']}]({info['url']})\n👤 作者: {info['author_mention']}\n\n{info['intro']}",
            color=0xff69b4
        )
        embed.set_author(name=info['author_name'], icon_url=info['author_avatar'])
        
        embed.add_field(name="📂 所属分区", value=info['category'], inline=True)
        embed.add_field(name="🏷️ 标签", value=" / ".join(info['tags']), inline=True)
        if info['image']: embed.set_image(url=info['image'])
        embed.set_footer(text="点击下方按钮抽取属于你的今日缘分！(每日限一次)")

        # 3. 根据模式执行动作
        if mode == "reset":
            # 模式 A：删除旧的，发新的
            await self._cleanup_old_messages(channel)
            await channel.send(embed=embed, view=DailyRecommendView())
        
        elif mode == "edit":
            # 模式 B：尝试编辑旧的
            target_msg = None
            try:
                async for msg in channel.history(limit=20):
                    if msg.author == self.bot.user and msg.embeds:
                        # 兼容检查
                        if "每日精选" in msg.embeds[0].title:
                            target_msg = msg
                            break
                
                if target_msg:
                    await target_msg.edit(embed=embed, view=DailyRecommendView())
                    print(f"Daily recommend updated (Edited) in {channel.id}")
                else:
                    await channel.send(embed=embed, view=DailyRecommendView())
                    print(f"Daily recommend sent (New) in {channel.id}")
            except Exception as e:
                print(f"Daily recommend update failed: {e}")

    @tasks.loop(time=time(hour=0, minute=0, tzinfo=TZ_SHANGHAI))
    async def daily_recommend_task(self):
        """每天0点自动刷新 (编辑模式)"""
        # 【修改】支持多频道推送
        for channel_id in DAILY_RECOMMEND_CHANNEL_ID:
            channel = self.bot.get_channel(channel_id)
            if channel:
                # 使用 mode="edit" 以保持频道整洁
                await self.refresh_recommendation_panel(channel, mode="edit")
                await asyncio.sleep(2) # 防止速率限制

    @daily_recommend_task.before_loop
    async def before_daily_task(self):
        await self.bot.wait_until_ready()

    # --- 手动调试命令 ---
    @app_commands.command(name="更新推荐面板", description="[管理] 强制刷新并重发今日推荐")
    @app_commands.default_permissions(view_audit_log=True)
    async def manual_recommend(self, interaction: discord.Interaction):
        is_admin = interaction.user.guild_permissions.administrator
        is_tester = False
        if isinstance(interaction.user, discord.Member) and interaction.user.get_role(TEST_ROLE_ID):
            is_tester = True
            
        if not (is_admin or is_tester):
            return await interaction.response.send_message("仅限管理员或测试员使用。", ephemeral=True)
            
        await interaction.response.defer(ephemeral=True)
        
        # 使用 mode="reset" (清理旧的，发新的)
        await self.refresh_recommendation_panel(interaction.channel, mode="reset")
        
        await interaction.followup.send("✅ 推荐面板已强制刷新！旧面板已清理。", ephemeral=True)

async def setup(bot):
    await bot.add_cog(RecommendCog(bot))
