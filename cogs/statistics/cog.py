import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timedelta, timezone, time
import asyncio

from . import db as statistics_db
from .views import ForumSelectView, StatisticsContainerView
from config import TZ_SHANGHAI

class StatisticsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.update_panels_task.start()

    async def cog_load(self):
        print("✅ [StatisticsCog] 已成功加载。")

    async def cog_unload(self):
        self.update_panels_task.cancel()

    @tasks.loop(time=time(hour=4, minute=0, tzinfo=TZ_SHANGHAI))
    async def update_panels_task(self):
        """定时任务：每天凌晨4点自动更新所有已创建的统计面板"""
        await self.bot.wait_until_ready()
        print("⏰ [StatisticsCog] 开始执行每日面板自动更新任务...")

        all_panels = await statistics_db.get_all_statistics_panels()
        if not all_panels:
            print("ℹ️ [StatisticsCog] 数据库中没有需要更新的面板。")
            return

        success_count = 0
        for panel_data in all_panels:
            try:
                guild = self.bot.get_guild(panel_data['guild_id'])
                if not guild: continue

                post_channel = guild.get_channel(panel_data['post_channel_id'])
                # 修正：如果post_channel也找不到了，应该跳过
                if not post_channel:
                    print(f"⚠️ [StatisticsCog] 找不到面板所在的频道 ID: {panel_data['post_channel_id']}，跳过。")
                    continue

                forum_channel = guild.get_channel(panel_data['forum_channel_id'])
                if not isinstance(forum_channel, discord.ForumChannel): continue

                message = await post_channel.fetch_message(panel_data['message_id'])

                stats_data = await self.gather_statistics(forum_channel)
                if stats_data:
                    new_view = StatisticsContainerView(stats_data)
                    await message.edit(view=new_view)
                    success_count += 1
                await asyncio.sleep(2)

            except discord.NotFound:
                await statistics_db.remove_statistics_panel(panel_data['message_id'])
            except Exception as e:
                print(f"❌ [StatisticsCog] 更新面板 (ID: {panel_data.get('message_id')}) 时出错: {e}")

        print(f"✅ [StatisticsCog] 每日面板更新任务完成，成功更新 {success_count}/{len(all_panels)} 个面板。")


    async def gather_statistics(self, forum: discord.ForumChannel):
        """
        收集单个论坛的统计数据。
        """
        if not forum:
            return None

        threads = forum.threads
        if not threads:
            return {
                "channel_name": forum.name, "channel_icon_url": forum.guild.icon.url if forum.guild.icon else None,
                "total_threads": 0, "new_threads_7d": 0, "hot_threads": [], "cold_threads": []
            }

        seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)
        total_threads = len(threads)
        new_threads_7d = sum(1 for t in threads if t.created_at > seven_days_ago)

        threads_with_stats = []
        for t in threads:
            starter = t.starter_message
            if not starter:
                try:
                    # --- 核心修复 ---
                    # `.flatten()` 已被移除，使用列表推导式来获取消息
                    history = [msg async for msg in t.history(limit=1, oldest_first=True)]
                    starter = history[0] if history else None
                    # --- 修复结束 ---
                except (discord.errors.Forbidden, IndexError):
                    starter = None

            likes = sum(r.count for r in starter.reactions) if starter else 0
            comments = t.message_count - 1 if t.message_count > 0 else 0
            author_name = t.owner.display_name if t.owner else "未知作者"
            tags = [tag.name for tag in t.applied_tags]

            threads_with_stats.append({
                "id": t.id, "name": t.name, "url": t.jump_url, "created_at": t.created_at,
                "likes": likes, "comments": comments, "score": likes * 1.5 + comments,
                "author_name": author_name, "tags": tags
            })

        threads_with_stats.sort(key=lambda x: x['score'], reverse=True)
        hot_threads = threads_with_stats[:15]

        now = datetime.now(timezone.utc)
        thirty_days_ago = now - timedelta(days=30)
        older_threads = [t for t in threads_with_stats if t['created_at'] < thirty_days_ago]
        older_threads.sort(key=lambda x: x['score'], reverse=False)
        cold_threads = older_threads[:15]

        return {
            "channel_name": forum.name, "channel_icon_url": forum.guild.icon.url if forum.guild.icon else None,
            "total_threads": total_threads, "new_threads_7d": new_threads_7d,
            "hot_threads": hot_threads, "cold_threads": cold_threads
        }

    # 斜杠命令
    statistics_group = app_commands.Group(name="统计", description="统计面板相关命令")

    @statistics_group.command(name="发送选择器", description="[管理] 发送一个用于创建统计面板的频道选择器")
    @app_commands.default_permissions(manage_guild=True)
    async def send_selector(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "请在下方选择要为其生成统计面板的论坛频道：",
            view=ForumSelectView(self),
            ephemeral=True
        )

    @statistics_group.command(name="清理无效面板", description="[管理] 清理数据库中已在Discord被删除的面板记录")
    @app_commands.default_permissions(manage_guild=True)
    async def cleanup_panels(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        all_panels = await statistics_db.get_all_statistics_panels()
        if not all_panels:
            return await interaction.followup.send("数据库中没有面板记录，无需清理。", ephemeral=True)

        cleaned_count = 0
        for panel_data in all_panels:
            try:
                channel = self.bot.get_channel(panel_data['post_channel_id'])
                if channel:
                    await channel.fetch_message(panel_data['message_id'])
                else:
                    raise discord.NotFound(None, "Channel not found")
            except discord.NotFound:
                await statistics_db.remove_statistics_panel(panel_data['message_id'])
                cleaned_count += 1

        await interaction.followup.send(f"✅ 清理完成！共移除了 **{cleaned_count}** 条无效的面板记录。", ephemeral=True)