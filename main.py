import discord
from discord.ext import commands
import os
from dotenv import load_dotenv
import aiohttp
import asyncio

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

# 将此代码放入 main.py，替换原来的 clearsync

@bot.command(name="clearsync")
@commands.is_owner()
async def clearsync(ctx):
    """
    【最终核弹版】彻底解决命令重复问题。
    它会同时清除「全局残留」和「本地缓存」，然后重新注册。
    """
    # 状态消息辅助函数
    status_msg = None
    async def safe_send(text):
        nonlocal status_msg
        try:
            if status_msg: await status_msg.edit(content=text)
            else: status_msg = await ctx.send(text)
        except: status_msg = await ctx.send(text)

    await safe_send("🧹 **开始执行彻底清理...**\n(请耐心等待，只需几秒)")

    try:
        # Step 1: 清除早已残留的【全局命令】(这是双胞胎的元凶！)
        # 这相当于告诉 Discord：“把贴在公共走廊上的旧海报全撕了”
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync(guild=None)
        await safe_send("🧹 进度 1/3: 🔥 已焚毁所有「全局命令」残留。\n(这应该能消灭重复的那个影子)")

        await asyncio.sleep(1) # 歇一秒让Discord反应过来

        # Step 2: 清除【当前服务器】的旧命令
        bot.tree.clear_commands(guild=ctx.guild)
        await bot.tree.sync(guild=ctx.guild)
        await safe_send("🧹 进度 2/3: 🏠 已清空「本服命令」缓存。\n(现在应该是个空壳了)")

        await asyncio.sleep(1)

        # Step 3: 重新把代码里的命令注册回来（只注册到本服，方便开发）
        # 将全局定义的命令复制到当前 Guild 空间下
        bot.tree.copy_global_to(guild=ctx.guild)
        synced = await bot.tree.sync(guild=ctx.guild)

        # 结束
        final_text = (
            f"✨ **大功告成！**\n"
            f"✅ 全局残留：**已清除** (重复项应该消失了)\n"
            f"✅ 本服重建：**{len(synced)}** 个命令\n"
            f"----------------------------------\n"
            f"⚠️ **最后一步（至关重要）：**\n"
            f"请现在立刻 **重启你的 Discord 软件** (Ctrl+R / 手机杀后台)。\n"
            f"这会让你的客户端重新去服务器拉取最新的列表。"
        )
        await safe_send(final_text)
        print(f"✅ [CLEARED] Global cleared. Guild {ctx.guild.id} synced {len(synced)} cmds.")

    except Exception as e:
        await safe_send(f"❌ 出错了: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    bot.run(TOKEN)
