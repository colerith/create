# main.py

import discord
from discord.ext import commands
import os
from dotenv import load_dotenv
import aiohttp

from cogs.core.db import close_db, init_db
from cogs.protection.views import BumpButtonView

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    print("错误：在 .env 文件中找不到 DISCORD_TOKEN。")
    exit()

TEST_GUILD_IDS = [1397629012292931726, 1384945301780955246, 1413953986519760908]
COG_EXTENSIONS = (
    "cogs.backup",
    "cogs.protection",
    "cogs.statistics",
    "cogs.recommend",
    "cogs.exploration",
)


class ChimidanBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self.http_session: aiohttp.ClientSession = None

    async def setup_hook(self):
        self.http_session = aiohttp.ClientSession()
        self.add_view(BumpButtonView(self))
        print("🔧 持久化视图 [BumpButtonView] 已注册。")

        await init_db()
        print("✅ 全局数据库检查完毕。")

        loaded_cogs = 0
        for extension in COG_EXTENSIONS:
            try:
                await self.load_extension(extension)
                print(f"📦 加载插件成功: {extension}")
                loaded_cogs += 1
            except Exception as e:
                print(f"❌ 加载插件失败 {extension}: {e}")

        commands_list = self.tree.get_commands()
        print(f"📊 [诊断] 目前 Bot 内存中已加载 {len(commands_list)} 个斜杠命令: {[cmd.name for cmd in commands_list]}")
        if len(commands_list) == 0:
            print("⚠️ 警告：Bot 内存中没有命令！请检查 cogs 里的代码是否正确编写。")

        # ContextMenu 与斜杠命令都必须同步到 Discord 才会出现在客户端。
        # 全局同步保证所有服务器、论坛帖子和普通文字频道都能收到同一套命令。
        try:
            synced_global = await self.tree.sync()
            print(f"🌐 全局应用命令同步完成，共 {len(synced_global)} 个。")
        except Exception as exc:
            print(f"❌ 全局应用命令同步失败: {exc}")

        # 测试服务器额外做 guild 同步，避免等待全局命令传播。
        for guild_id in TEST_GUILD_IDS:
            guild = discord.Object(id=guild_id)
            try:
                self.tree.copy_global_to(guild=guild)
                synced_guild = await self.tree.sync(guild=guild)
                print(
                    f"🧪 测试服务器 {guild_id} 应用命令同步完成，共 {len(synced_guild)} 个。"
                )
            except Exception as exc:
                print(f"⚠️ 测试服务器 {guild_id} 应用命令同步失败: {exc}")

    async def close(self):
        if self.http_session:
            await self.http_session.close()
        await close_db()
        await super().close()

bot = ChimidanBot()

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user} (ID: {bot.user.id})')
    print('------')

    print(f"🌐 Bot 当前已加入 {len(bot.guilds)} 个服务器，应用命令已在启动阶段同步。")


@bot.command(name="forcesync")
@commands.is_owner()
async def forcesync(ctx: commands.Context):
    """手动强制同步命令到当前服务器"""
    msg = await ctx.send("🚑 正在执行手动急救同步...")
    try:
        if not ctx.guild:
            return await msg.edit(content="此命令不能在私信中使用。")
        bot.tree.copy_global_to(guild=ctx.guild)
        synced = await bot.tree.sync(guild=ctx.guild)
        await msg.edit(content=f"✅ **急救完成！**\n已强制注册 **{len(synced)}** 个命令。\n请立即 **重启 Discord** (Ctrl+R) 刷新缓存。")
    except Exception as e:
        await msg.edit(content=f"❌ 失败: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    bot.run(TOKEN)
