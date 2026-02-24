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

    async def refresh_recommendation_panel(self, channel: discord.TextChannel, mode: str = "edit"):
        """
        刷新推荐面板 (Container 版) - 采用更健壮的“先清理后执行”模式。
        'edit': 编辑最新的一个, 并删除所有其他多余的旧面板。
        'reset': 删除所有旧面板, 再发送一个新的。
        """
        print(f"🔄 [Recommend] Action: Refresh panel in '{channel.name}' (mode: {mode}).")

        # 步骤 1: 准备新视图
        pool = await utils.get_random_thread_pool(channel.guild)
        new_view = DailyRecommendContainer(
            await utils.fetch_thread_details(random.choice(pool)) if pool else {},
            is_empty=not pool
        )

        # 步骤 2: 查找所有旧面板
        old_panels = []
        try:
            async for msg in channel.history(limit=50):
                if msg.author == self.bot.user and msg.components:
                    is_panel = any(
                        getattr(child, 'custom_id', None) == 'daily_gacha_open_btn'
                        for action_row in msg.components
                        for child in action_row.children
                    )
                    if is_panel:
                        old_panels.append(msg)
        except discord.Forbidden:
            print(f"❌ [Recommend] No permission to read history in '{channel.name}'. Aborting.")
            return
        except Exception as e:
            print(f"⚠️ [Recommend] Error scanning for old panels in '{channel.name}': {e}")

        print(f"🔍 [Recommend] Found {len(old_panels)} old panel(s).")

        # 步骤 3: 确定要编辑和删除的目标
        target_to_edit = None
        panels_to_delete = []

        if mode == 'edit' and old_panels:
            target_to_edit = old_panels.pop(0)  # 最新的作为编辑目标
            panels_to_delete.extend(old_panels) # 其他所有都删除
            print(f"📝 [Recommend] 'edit' mode: Target msg {target_to_edit.id} for edit, {len(panels_to_delete)} for deletion.")
        else:  # 'reset' 模式或 'edit' 模式但没找到旧面板
            panels_to_delete.extend(old_panels)
            print(f"🗑️ [Recommend] 'reset' mode: Target {len(panels_to_delete)} for deletion.")

        # 步骤 4: 执行删除
        if panels_to_delete:
            delete_tasks = [panel.delete() for panel in panels_to_delete]
            await asyncio.gather(*delete_tasks, return_exceptions=True)
            print(f"✅ [Recommend] Cleanup complete. Deleted {len(panels_to_delete)} old panel(s).")

        # 步骤 5: 执行编辑或发送
        if target_to_edit:
            try:
                await target_to_edit.edit(view=new_view)
                print(f"✅ [Recommend] Successfully edited panel {target_to_edit.id}.")
                return  # 成功编辑，任务完成
            except discord.NotFound:
                print(f"ℹ️ [Recommend] Panel {target_to_edit.id} not found for editing. Will send a new one.")
                # 消息已被删除，将继续执行下面的发送逻辑
            except Exception as e:
                print(f"❌ [Recommend] Failed to edit panel {target_to_edit.id}: {e}. Will send a new one.")
                # 其他编辑错误，也将继续执行下面的发送逻辑

        # 如果需要发送新面板（重置模式、编辑目标不存在或编辑失败）
        try:
            await channel.send(view=new_view)
            print(f"✅ [Recommend] Sent a new panel to '{channel.name}'.")
        except Exception as e:
            print(f"❌ [Recommend] Failed to send new panel to '{channel.name}': {e}")


    @tasks.loop(time=time(hour=0, minute=1, tzinfo=TZ_SHANGHAI))
    async def daily_recommend_task(self):
        for channel_id in RECOMMEND_DAILY_CHANNEL_IDS:
            channel = self.bot.get_channel(channel_id)
            if not channel:
                try: channel = await self.bot.fetch_channel(channel_id)
                except (discord.NotFound, discord.Forbidden):
                    print(f"⚠️ [Recommend] Daily task can't find/access channel ID: {channel_id}")
                    continue

            if isinstance(channel, discord.TextChannel):
                # 每日任务总是使用 'edit' 模式来保证只存在一个面板
                await self.refresh_recommendation_panel(channel, mode="edit")
                await asyncio.sleep(2)


    @daily_recommend_task.before_loop
    async def before_daily_task(self):
        await self.bot.wait_until_ready()
        print("👍 [RecommendCog] Bot ready, daily recommendation task waiting.")


    @app_commands.command(name="更新推荐面板", description="[管理] 刷新本频道的今日推荐内容")
    @app_commands.describe(mode="选择刷新模式：'刷新内容' (默认) 或 '重置面板' (清理所有旧面板)")
    @app_commands.choices(mode=[
        app_commands.Choice(name="刷新内容 (编辑最新的, 清理多余的)", value="edit"),
        app_commands.Choice(name="重置面板 (删除所有旧面板并新建)", value="reset")
    ])
    @app_commands.default_permissions(manage_guild=True)
    async def manual_recommend(self, interaction: discord.Interaction, mode: str = "edit"):
        is_admin = interaction.user.guild_permissions.administrator
        if not is_admin:
            return await interaction.response.send_message("仅限管理员使用。", ephemeral=True)

        if not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message("此命令只能在文字频道使用。", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        await self.refresh_recommendation_panel(interaction.channel, mode=mode)

        if mode == "reset":
            await interaction.followup.send("✅ 已重置面板！所有旧面板均已清理。", ephemeral=True)
        else:
            await interaction.followup.send("✅ 面板已刷新！", ephemeral=True)
