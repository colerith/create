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

    async def refresh_recommendation_panel(self, channel: discord.TextChannel, resend_all: bool = False):
        """
        刷新推荐面板 (Container 版) - 智能编辑或重发模式

        :param channel: 目标文字频道
        :param resend_all: 如果为 True, 会删除频道内所有旧面板并发送一个新的。
        """
        # --- 步骤 1: 准备新的 View ---
        pool = await utils.get_random_thread_pool(channel.guild)
        new_view = None
        if not pool:
            # 空状态
            new_view = DailyRecommendContainer({}, is_empty=True)
        else:
            target_thread = random.choice(pool)
            info = await utils.fetch_thread_details(target_thread)
            new_view = DailyRecommendContainer(info)

        # --- 步骤 2: 查找和处理旧面板 ---
        target_msg = None
        old_messages_to_delete = []
        try:
            # 扫描最近 50 条消息以查找所有旧面板
            async for msg in channel.history(limit=50):
                if msg.author == self.bot.user:
                    # 使用更可靠的检查方法
                    if msg.components and any(
                        getattr(child, 'custom_id', None) == 'daily_gacha_open_btn'
                        for action_row in msg.components
                        for child in action_row.children
                    ):
                        if resend_all:
                            old_messages_to_delete.append(msg)
                        elif target_msg is None: # 只取最新的一个作为编辑目标
                            target_msg = msg
                        else: # 其他更旧的都加入删除列表
                            old_messages_to_delete.append(msg)

        except discord.Forbidden:
            print(f"❌ [Recommend] 权限不足，无法读取频道 {channel.name} 的历史记录。")
            return # 没有权限则直接中止
        except Exception as e:
            print(f"🔍 [Recommend] 扫描推荐面板历史时出错: {e}")

        # --- 步骤 3: 批量删除标记的旧消息 ---
        if old_messages_to_delete:
            try:
                # 只在文字频道和论坛帖内使用 bulk_delete
                if isinstance(channel, (discord.TextChannel, discord.Thread)) and len(old_messages_to_delete) > 1:
                     await channel.delete_messages(old_messages_to_delete)
                else:
                    for m in old_messages_to_delete:
                        await m.delete()
                        await asyncio.sleep(0.5) # 防止速率限制
            except Exception as e:
                print(f"🗑️ [Recommend] 删除旧面板时失败: {e}")


        # --- 步骤 4: 执行更新或发送 ---
        # 如果不是强制重发模式，并且找到了一个可编辑的面板
        if not resend_all and target_msg:
            try:
                # 尝试编辑最新的那个旧面板
                await target_msg.edit(view=new_view)
                print(f"✅ [Recommend] 成功编辑了频道 {channel.name} 的推荐面板。")
                return # 编辑成功，任务完成
            except discord.NotFound:
                # 消息已经被删了，接下来会发送新的
                print(f"ℹ️ [Recommend] 准备编辑的面板 (ID: {target_msg.id}) 已被删除，将发送新面板。")
                pass
            except Exception as e:
                print(f"⚠️ [Recommend] 编辑推荐面板失败: {e}。将尝试发送新面板。")


        # --- 步骤 5: 发送新面板 (如果编辑不成功或需要强制重发) ---
        try:
            await channel.send(view=new_view)
            print(f"✅ [Recommend] 已在频道 {channel.name} 发送了新的推荐面板。")
        except discord.Forbidden:
            print(f"❌ [Recommend] 权限不足，无法在频道 {channel.name} 发送消息。")
        except Exception as e:
            print(f"❌ [Recommend] 发送新推荐面板时发生未知错误: {e}")


    @tasks.loop(time=time(hour=0, minute=1, tzinfo=TZ_SHANGHAI)) # 改为0点1分，避开整点高峰
    async def daily_recommend_task(self):
        print(f"⏰ [Recommend] 开始执行每日推荐任务...")
        for channel_id in RECOMMEND_DAILY_CHANNEL_IDS:
            channel = self.bot.get_channel(channel_id)
            if not channel:
                try:
                    channel = await self.bot.fetch_channel(channel_id)
                except (discord.NotFound, discord.Forbidden):
                    print(f"⚠️ [Recommend] 每日任务无法找到或访问频道 ID: {channel_id}")
                    continue

            if isinstance(channel, discord.TextChannel):
                 # 每日自动任务，总是尝试编辑，而不是强制重发
                await self.refresh_recommendation_panel(channel, resend_all=False)
                await asyncio.sleep(2) # 友好等待
        print("✅ [Recommend] 每日推荐任务执行完毕。")


    @daily_recommend_task.before_loop
    async def before_daily_task(self):
        await self.bot.wait_until_ready()
        print("👍 [RecommendCog] Bot 已就绪, 每日推荐任务等待启动...")


    @app_commands.command(name="更新推荐面板", description="[管理] 强制刷新本频道的今日推荐内容")
    @app_commands.describe(mode="选择刷新模式：'刷新内容' (默认) 或 '重置面板' (清理所有旧面板)")
    @app_commands.choices(mode=[
        app_commands.Choice(name="刷新内容 (编辑旧有面板)", value="edit"),
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

        resend = (mode == "reset")
        await self.refresh_recommendation_panel(interaction.channel, resend_all=resend)

        if resend:
            await interaction.followup.send("✅ 所有旧推荐面板已清理，并发布了全新的面板！", ephemeral=True)
        else:
            await interaction.followup.send("✅ 推荐面板内容已刷新！", ephemeral=True)