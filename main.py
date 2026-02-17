import discord
from discord.ext import commands
import os
from dotenv import load_dotenv
import aiohttp
import asyncio

# 确保你的数据库导入路径是正确的
from cogs.protection.db import init_db

load_dotenv()
TEST_GUILD_ID = [1397629012292931726, 1384945301780955246, 1413953986519760908]

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    print("错误：在 .env 文件中找不到 DISCORD_TOKEN。")
    print("请确保 .env 文件存在于项目根目录，并且内容格式为：DISCORD_TOKEN=你的BotToken")
    exit()

class ChimidanBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        # 显式指定 application_id 也是个好习惯，不过通常不需要
        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self.http_session: aiohttp.ClientSession = None

    async def setup_hook(self):
        self.http_session = aiohttp.ClientSession()
        await init_db()
        print("✅ 数据库检查完毕。")

        # --- 加载插件 ---
        loaded_cogs = 0
        for item in os.listdir('./cogs'):
            if item.startswith('__'): continue
            path = os.path.join('./cogs', item)

            ext_name = None
            if os.path.isfile(path) and item.endswith('.py'):
                ext_name = f'cogs.{item[:-3]}'
            elif os.path.isdir(path):
                if os.path.exists(os.path.join(path, 'cog.py')):
                    ext_name = f'cogs.{item}.cog'
                else:
                    ext_name = f'cogs.{item}'

            if ext_name:
                try:
                    await self.load_extension(ext_name)
                    print(f"📦 加载插件成功: {ext_name}")
                    loaded_cogs += 1
                except Exception as e:
                    print(f"❌ 加载插件失败 {ext_name}: {e}")

        # --- 关键诊断 ---
        # 看看Bot现在肚子里到底有几个命令
        # get_commands() 获取的是全局命令缓存
        commands_list = self.tree.get_commands()
        print(f"📊 [诊断] 目前 Bot 内存中已加载 {len(commands_list)} 个斜杠命令: {[cmd.name for cmd in commands_list]}")

        if len(commands_list) == 0:
            print("⚠️ 警告：Bot 内存中没有命令！这也难怪同步也没用了。请检查 cogs 里的代码是否写了 @app_commands.command()")

    async def close(self):
        await super().close()
        if self.http_session:
            await self.http_session.close()

bot = ChimidanBot()

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user} (ID: {bot.user.id})')
    print('------')

    # 妈妈加上了这个自动修复逻辑
    # 只要 Bot 一启动，它就会试着把命令推送到它能看见的第一个公会
    if len(bot.guilds) > 0:
        target_guild = bot.guilds[0] # 取第一个服务器，通常就是你的测试服
        print(f"🚑 [紧急修复] 正在尝试强制同步命令到服务器: {target_guild.name} (ID: {target_guild.id})...")

        try:
            # 1. 再次清除，确保无残留
            bot.tree.clear_commands(guild=target_guild)

            # 2. 复制全局命令到这个公会
            bot.tree.copy_global_to(guild=target_guild)

            # 3. 同步
            synced = await bot.tree.sync(guild=target_guild)
            print(f"✅ [恢复成功] 已成功向 {target_guild.name} 注册了 {len(synced)} 个命令！")
            print("👉 现在请重启 Discord 客户端，你应该能看到命令了。")
        except Exception as e:
            print(f"❌ [恢复失败] 同步出错: {e}")
    else:
        print("❌ Bot 还没有加入任何服务器，无法执行同步。")

# 保留这个手动急救命令
@bot.command(name="forcesync")
@commands.is_owner()
async def forcesync(ctx):
    """手动强制同步命令到当前服务器"""
    msg = await ctx.send("🚑 正在执行手动急救同步...")
    try:
        bot.tree.clear_commands(guild=ctx.guild)
        bot.tree.copy_global_to(guild=ctx.guild)
        synced = await bot.tree.sync(guild=ctx.guild)
        await msg.edit(content=f"✅ **急救完成！**\n已强制注册 **{len(synced)}** 个命令。\n请立即 **重启 Discord** (Ctrl+R) 刷新缓存。")
        print(f"Manual sync to {ctx.guild.id}: {len(synced)} cmds.")
    except Exception as e:
        await msg.edit(content=f"❌ 失败: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    bot.run(TOKEN)
