# main.py

import discord
from discord.ext import commands
import os
from dotenv import load_dotenv
import aiohttp

from cogs.core.db import init_db
from cogs.protection.views import BumpButtonView

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    print("错误：在 .env 文件中找不到 DISCORD_TOKEN。")
    exit()

TEST_GUILD_ID = [1397629012292931726, 1384945301780955246, 1413953986519760908]
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

    async def close(self):
        await super().close()
        if self.http_session:
            await self.http_session.close()

bot = ChimidanBot()

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user} (ID: {bot.user.id})')
    print('------')

    if bot.guilds:
        target_guild = bot.guilds[0]
        print(f"🚑 [紧急修复] 正在尝试强制同步命令到服务器: {target_guild.name} (ID: {target_guild.id})...")
        try:
            bot.tree.clear_commands(guild=target_guild)
            bot.tree.copy_global_to(guild=target_guild)
            synced = await bot.tree.sync(guild=target_guild)
            print(f"✅ [恢复成功] 已成功向 {target_guild.name} 注册了 {len(synced)} 个命令！")
        except Exception as e:
            print(f"❌ [恢复失败] 同步出错: {e}")
    else:
        print("❌ Bot 还没有加入任何服务器，无法执行同步。")


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
