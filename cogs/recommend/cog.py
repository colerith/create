#cogs/recommend/cog.py

import discord
from discord import app_commands, ui
from discord.ext import commands, tasks
import aiosqlite
import random
import asyncio
from datetime import datetime, time

from core.db import get_db
from config import TZ_SHANGHAI, RECOMMEND_TARGET_KEYWORDS, RECOMMEND_DAILY_CHANNEL_IDS, RECOMMEND_TEST_ROLE_ID, TARGET_KEYWORDS, TEST_ROLE_ID

from views import DailyRecommendView

class RecommendCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # 实例化视图时，把 cog 自身 (self) 传进去
        self.bot.add_view(DailyRecommendView(self))
        self.daily_recommend_task.start()

    async def cog_unload(self):
        self.daily_recommend_task.cancel()

    # --- 辅助方法 (被 Cog 内部使用) ---

    def get_card_forums(self, guild: discord.Guild):
        """获取所有包含目标关键词的论坛频道"""
        return [c for c in guild.forums if any(keyword in c.name for keyword in RECOMMEND_TARGET_KEYWORDS)]

    async def get_random_thread_pool(self, guild: discord.Guild, specific_channel_id=None):
        """获取符合条件的帖子池 (排除置顶帖)"""
        forums = self.get_card_forums(guild)
        if specific_channel_id:
            forums = [f for f in forums if f.id == int(specific_channel_id)]

        threads = [t for forum in forums for t in forum.threads if not t.flags.pinned]
        return threads

    async def fetch_thread_details(self, thread: discord.Thread):
        """获取帖子的详细信息"""
        try:
            starter = thread.starter_message or (await thread.history(limit=1, oldest_first=True).flatten())[0]
        except (discord.errors.Forbidden, IndexError):
            starter = None

        intro, image_url = "（暂无介绍）", None
        if starter:
            intro = starter.content[:300] + "..." if len(starter.content) > 300 else starter.content
            image_url = next((att.url for att in starter.attachments if att.content_type and "image" in att.content_type), None)

        tags = [tag.name for tag in thread.applied_tags] or ["无标签"]
        owner = thread.owner

        return {
            "title": thread.name, "author_name": owner.display_name if owner else "未知作者",
            "author_mention": owner.mention if owner else "未知作者", "author_avatar": owner.display_avatar.url if owner else None,
            "intro": intro, "category": thread.parent.name, "tags": tags, "url": thread.jump_url, "image": image_url
        }

    # --- 抽卡核心逻辑 (由 UI 视图调用) ---

    async def execute_draw(self, interaction: discord.Interaction, count: int, channel_id: int = None):
        is_tester = isinstance(interaction.user, discord.Member) and bool(interaction.user.get_role(RECOMMEND_TEST_ROLE_ID))

        # 1. 检查抽卡次数
        if not is_tester:
            today_str = datetime.now(TZ_SHANGHAI).strftime("%Y-%m-%d")
            async with get_db() as db:
                cursor = await db.execute("SELECT 1 FROM daily_gacha_records WHERE user_id = ? AND last_draw_date = ?", (interaction.user.id, today_str))
                if await cursor.fetchone():
                    return await interaction.response.send_message("🔮 您今天已经感应过缘分啦，请明天再来吧！", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        # 2. 获取卡池并抽卡
        threads = await self.get_random_thread_pool(interaction.guild, channel_id)
        if not threads:
            return await interaction.followup.send("🏜️ 当前选择的卡池里空空如也... (或是只有置顶帖)", ephemeral=True)

        count = min(count, len(threads))
        drawn_threads = random.sample(threads, count)

        # 3. 标记抽卡
        if not is_tester:
            today_str = datetime.now(TZ_SHANGHAI).strftime("%Y-%m-%d")
            async with get_db() as db:
                await db.execute("INSERT OR REPLACE INTO daily_gacha_records (user_id, last_draw_date) VALUES (?, ?)", (interaction.user.id, today_str))
                await db.commit()

        # 4. 发送结果
        embeds = []
        footer_text = f"点击蓝色标题即可跳转详情{' (测试员模式)' if is_tester else ''}"

        if count == 1:
            info = await self.fetch_thread_details(drawn_threads[0])
            embed = discord.Embed(title=f"✨ 命运的邂逅：{info['title']}", description=f"👤 作者: {info['author_mention']}\n\n{info['intro']}", color=0xffd700, url=info['url'])
            embed.set_author(name=info['author_name'], icon_url=info['author_avatar'])
            embed.add_field(name="📂 分区", value=info['category'], inline=True).add_field(name="🏷️ 标签", value=" / ".join(info['tags']), inline=True)
            if info['image']: embed.set_image(url=info['image'])
            embed.set_footer(text="今日缘分已定！" + (' (测试员模式：不消耗次数)' if is_tester else ''))
            embeds.append(embed)
        else:
            main_embed = discord.Embed(title=f"💫 恭喜获得 {count} 连抽结果！", color=0xff69b4)
            desc_text = ""
            for i, t in enumerate(drawn_threads):
                tags_str = f"[{' '.join([tag.name for tag in t.applied_tags[:3]])}]" if t.applied_tags else ""
                desc_text += f"{i+1}. **[{t.name}]({t.jump_url})** - {t.owner.display_name if t.owner else '未知'} {tags_str}\n"
            main_embed.description = desc_text
            main_embed.set_footer(text=footer_text)
            embeds.append(main_embed)

        await interaction.followup.send(embeds=embeds, ephemeral=True)

    # --- 每日推荐面板逻辑 ---

    async def refresh_recommendation_panel(self, channel, mode="edit"):
        pool = await self.get_random_thread_pool(channel.guild)
        if not pool:
            if mode == "reset": # 仅在手动重置时发送空池提示
                await channel.send(embed=discord.Embed(title="📅 每日推荐", description="今天资源库里空空如也捏...", color=0x99aab5))
            return

        target_thread = random.choice(pool)
        info = await self.fetch_thread_details(target_thread)
        date_str = datetime.now(TZ_SHANGHAI).strftime("%m月%d日")

        embed = discord.Embed(
            title=f"📅 {date_str} · 每日精选",
            description=f"### [{info['title']}]({info['url']})\n👤 作者: {info['author_mention']}\n\n{info['intro']}",
            color=0xff69b4
        )
        embed.set_author(name=info['author_name'], icon_url=info['author_avatar'])
        embed.add_field(name="📂 所属分区", value=info['category'], inline=True).add_field(name="🏷️ 标签", value=" / ".join(info['tags']), inline=True)
        if info['image']: embed.set_image(url=info['image'])
        embed.set_footer(text="点击下方按钮抽取属于你的今日缘分！(每日限一次)")

        view = DailyRecommendView(self)

        # 清理旧消息
        if mode == "reset":
            async for msg in channel.history(limit=20):
                if msg.author == self.bot.user and msg.embeds and "每日精选" in msg.embeds[0].title:
                    await msg.delete(); await asyncio.sleep(0.5)
            await channel.send(embed=embed, view=view)
            return

        # 编辑或发送
        try:
            target_msg = None
            async for msg in channel.history(limit=20):
                if msg.author == self.bot.user and msg.embeds and "每日精选" in msg.embeds[0].title:
                    target_msg = msg; break

            if target_msg: await target_msg.edit(embed=embed, view=view)
            else: await channel.send(embed=embed, view=view)
        except Exception as e: print(f"Daily recommend update failed: {e}")

    # --- 后台任务与命令 ---

    @tasks.loop(time=time(hour=0, minute=0, tzinfo=TZ_SHANGHAI))
    async def daily_recommend_task(self):
        for channel_id in RECOMMEND_DAILY_CHANNEL_IDS:
            if channel := self.bot.get_channel(channel_id):
                await self.refresh_recommendation_panel(channel, mode="edit")
                await asyncio.sleep(2)

    @daily_recommend_task.before_loop
    async def before_daily_task(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="更新推荐面板", description="[管理] 强制刷新并重发今日推荐")
    @app_commands.default_permissions(view_audit_log=True)
    async def manual_recommend(self, interaction: discord.Interaction):
        is_admin = interaction.user.guild_permissions.administrator
        is_tester = isinstance(interaction.user, discord.Member) and bool(interaction.user.get_role(RECOMMEND_TEST_ROLE_ID))
        if not (is_admin or is_tester):
            return await interaction.response.send_message("仅限管理员或测试员使用。", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        await self.refresh_recommendation_panel(interaction.channel, mode="reset")
        await interaction.followup.send("✅ 推荐面板已强制刷新！", ephemeral=True)

