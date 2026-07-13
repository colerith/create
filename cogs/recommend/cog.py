import discord
from discord import app_commands
from discord.ext import commands, tasks
import asyncio
import random
from datetime import datetime, time

from . import db
from . import utils
from .views import DailyRecommendContainer

from config import TZ_SHANGHAI, RECOMMEND_DAILY_CHANNEL_IDS, TEST_ROLE_ID

class RecommendCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.loop.create_task(db.init_recommend_db())
        self.daily_recommend_task.start()

    async def cog_unload(self):
        self.daily_recommend_task.cancel()

    async def refresh_recommendation_panel(self, channel: discord.TextChannel):
        """
        刷新推荐面板, 使用数据库进行追踪。
        """
        print(f"🔄 [Recommend] Action: Refresh panel in '{channel.name}'.")

        # 步骤 1: 准备新视图
        pool = await utils.get_random_thread_pool(channel.guild)
        new_view = DailyRecommendContainer(
            await utils.fetch_thread_details(random.choice(pool)) if pool else {},
            is_empty=not pool
        )

        # 步骤 2: 从数据库获取旧面板ID
        message_id = await db.get_panel_message_id(channel.id)
        target_msg = None

        if message_id:
            try:
                # 尝试精确获取消息对象
                target_msg = await channel.fetch_message(message_id)
                print(f"🔍 [Recommend] Found panel message {message_id} from DB.")
            except discord.NotFound:
                print(f"ℹ️ [Recommend] Panel message {message_id} from DB not found. It was likely deleted.")
                # 消息已被删除，清除数据库中的无效记录
                await db.remove_panel_message(channel.id)
            except discord.Forbidden:
                print(f"❌ [Recommend] No permission to fetch message {message_id}. Cannot refresh.")
                return # 没有权限，无法继续
            except Exception as e:
                print(f"⚠️ [Recommend] Error fetching message {message_id}: {e}")

        # 步骤 3: 执行编辑或发送
        if target_msg:
            # 如果成功获取到消息，则编辑它
            try:
                await target_msg.edit(
                    view=new_view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                print(f"✅ [Recommend] Successfully edited panel {target_msg.id}.")
            except Exception as e:
                print(f"❌ [Recommend] Failed to edit panel {target_msg.id}: {e}. Will send a new one.")
                # 编辑失败，可能是其他问题，当作没找到处理，下面会发送新的
                target_msg = None # 重置目标，确保会发送新消息

        if not target_msg:
            # 如果没有找到旧消息，或者编辑失败，则发送一个新的
            try:
                new_msg = await channel.send(
                    view=new_view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                print(f"✅ [Recommend] Sent a new panel {new_msg.id} to '{channel.name}'.")
                # 将新消息的ID存入数据库
                await db.set_panel_message(channel.id, new_msg.id)
            except discord.Forbidden:
                 print(f"❌ [Recommend] No permission to send message in '{channel.name}'.")
            except Exception as e:
                print(f"❌ [Recommend] Failed to send new panel: {e}")


    @tasks.loop(time=time(hour=0, minute=1, tzinfo=TZ_SHANGHAI))
    async def daily_recommend_task(self):
        print(f"⏰ [Recommend] Starting daily recommendation task...")
        for channel_id in RECOMMEND_DAILY_CHANNEL_IDS:
            channel = self.bot.get_channel(channel_id)
            if not channel:
                try: channel = await self.bot.fetch_channel(channel_id)
                except (discord.NotFound, discord.Forbidden):
                    print(f"⚠️ [Recommend] Daily task can't find/access channel ID: {channel_id}")
                    continue

            if isinstance(channel, discord.TextChannel):
                await self.refresh_recommendation_panel(channel)
                await asyncio.sleep(2)
        print("✅ [Recommend] Daily recommendation task finished.")

    @daily_recommend_task.before_loop
    async def before_daily_task(self):
        await self.bot.wait_until_ready()
        print("👍 [RecommendCog] Bot ready, daily recommendation task waiting.")


    @app_commands.command(name="更新推荐面板", description="[管理] 强制刷新本频道的今日推荐内容")
    @app_commands.default_permissions(manage_guild=True)
    async def manual_recommend(self, interaction: discord.Interaction):
        is_admin = interaction.user.guild_permissions.administrator
        if not is_admin:
            return await interaction.response.send_message("仅限管理员使用。", ephemeral=True)

        if not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message("此命令只能在文字频道使用。", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        exploration_cog = self.bot.get_cog("ExplorationCog")
        if exploration_cog and hasattr(exploration_cog, "rebuild_ordered_public_panels"):
            await exploration_cog.rebuild_ordered_public_panels(interaction.channel, include_recommend=True)
            await interaction.followup.send("✅ 公开面板已按固定顺序重建。", ephemeral=True)
            return

        await self.refresh_recommendation_panel(interaction.channel)
        await interaction.followup.send("✅ 面板已刷新！", ephemeral=True)
