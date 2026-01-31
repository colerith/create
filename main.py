import discord
from discord.ext import commands
import os
from dotenv import load_dotenv
import aiohttp

# 确保你的数据库导入路径是正确的
from database import init_db
from cogs.protection.db import init_likes_db

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    print("错误：在 .env 文件中找不到 DISCORD_TOKEN。")
    print("请确保 .env 文件存在于项目根目录，并且内容格式为：DISCORD_TOKEN=你的BotToken")
    exit()

class ChimidanBot(commands.Bot):
    def __init__(self):
        # 记得开启所有权限，这样Bot才能灵敏地接收消息
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents, help_command=None)

        self.http_session: aiohttp.ClientSession = None

    async def setup_hook(self):
        self.http_session = aiohttp.ClientSession()

        # 初始化数据库
        await init_db()
        await init_likes_db()
        print("✅ 数据库表检查完毕。")

        # --- 妈妈修改后的加载逻辑 ---
        # 它可以更智能地找到你的 cog.py
        for item in os.listdir('./cogs'):
            # 忽略 __pycache__ 等无关文件
            if item.startswith('__'): continue

            path = os.path.join('./cogs', item)

            if os.path.isfile(path) and item.endswith('.py'):
                # 情况1: cogs/xxx.py
                ext_name = f'cogs.{item[:-3]}'
                try:
                    await self.load_extension(ext_name)
                    print(f"📦 已加载单文件插件: {ext_name}")
                except Exception as e:
                    print(f"❌ 加载失败 {ext_name}: {e}")

            elif os.path.isdir(path):

                if os.path.exists(os.path.join(path, 'cog.py')):
                    ext_name = f'cogs.{item}.cog' # 指向 cogs.protection.cog
                else:
                    ext_name = f'cogs.{item}' # 指向 cogs.protection (__init__)

                try:
                    await self.load_extension(ext_name)
                    print(f"📦 已加载文件夹插件: {ext_name}")
                except Exception as e:
                    print(f"❌ 加载失败 {ext_name}: {e}")

        print(f"🚀 {self.user} 准备就绪！")

    async def close(self):
        await super().close()
        if self.http_session:
            await self.http_session.close()

bot = ChimidanBot()

@bot.command(name="sync")
@commands.is_owner() # 确保只有你能用
async def sync(ctx):
    """
    强力同步：清除当前服务器的旧命令缓存，并重新同步。
    解决命令重复或不显示的问题。
    """
    print(f"🔄 收到强力同步指令，来自: {ctx.author}")
    async with ctx.typing():
        try:
            # 1. 先清除当前公会的命令（解决重复显示的问题）
            bot.tree.clear_commands(guild=ctx.guild)

            # 2. 从全局 Tree 复制最新的命令副本到当前公会
            bot.tree.copy_global_to(guild=ctx.guild)

            # 3. 执行同步
            synced = await bot.tree.sync(guild=ctx.guild)

            await ctx.send(
                f"✅ **同步与清理完成！**\n"
                f"已清除旧缓存，并重新注册了 **{len(synced)}** 个命令到本服务器。\n"
                f"现在的结构应该是：\n"
                f"- `/贴主 ...` (包含置底附件)\n"
                f"- `/保护附件 ...`\n"
                f"- `/管理员专用 ...`"
            )
            print(f"✅ Guild {ctx.guild.id} 同步完成，共 {len(synced)} 个命令。")

        except Exception as e:
            await ctx.send(f"❌ 同步失败: {e}")
            print(f"❌ 同步出错: {e}")

if __name__ == "__main__":
    bot.run(TOKEN)
