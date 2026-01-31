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
    【容错版核弹清理】彻底刷新命令缓存。
    即使消息丢失也能正常反馈结果。
    """
    # 1. 先发一条初始消息
    try:
        status_msg = await ctx.send("🧹 **正在执行深度清理...**\n⏳ 第一步：正在抹除旧命令缓存...")
    except:
        # 如果连发消息都失败，直接不跑了（极少见）
        return

    # 定义一个更稳健的更新函数，防止消息被删导致报错
    async def safe_update(text):
        nonlocal status_msg
        try:
            await status_msg.edit(content=text)
        except discord.NotFound:
            # 如果原来的消息找不到了（报错404），就发条新的
            status_msg = await ctx.send(text)
        except Exception as e:
            # 其他情况也发新的
            status_msg = await ctx.send(f"{text}\n(PS: 之前消息更新失败: {e})")

    try:
        # --- 核心清理逻辑 ---

        # 步骤 1: 强制清空当前服务器的命令树
        # 这会告诉 Discord：“这个服务器现在没有任何命令”
        bot.tree.clear_commands(guild=ctx.guild)
        await bot.tree.sync(guild=ctx.guild)

        # 更新进度
        await safe_update("🧹 **正在执行深度清理...**\n✅ 第一步完成：旧命令已全部标记为删除。\n⏳ 第二步：正在加载最新代码并重新上传...")

        # 稍微歇一口气，防止 API 请求过快
        await asyncio.sleep(2)

        # 步骤 2: 将代码里的命令重新加载进来
        bot.tree.copy_global_to(guild=ctx.guild)

        # 步骤 3: 再次同步，上传正确版本
        synced = await bot.tree.sync(guild=ctx.guild)

        # --- 结束报告 ---
        final_msg = (
            f"✨ **清理完毕！命令树已重建。**\n"
            f"📊 重新注册命令数：**{len(synced)}** 个。\n"
            f"----------------------------------\n"
            f"现在你应该只能看到以下 3 个主命令组：\n"
            f"1️⃣ `/贴主` (包含置底功能)\n"
            f"2️⃣ `/保护附件` (下载与管理)\n"
            f"3️⃣ `/管理员专用` (配置与迁移)\n\n"
            f"⚠️ **重要提示**：\n"
            f"如果你还能看到其他奇怪的命令，请务必 **重启你的 Discord 客户端** (Ctrl+R / 杀后台重启 APP)。"
        )
        await safe_update(final_msg)
        print(f"✅ Guild {ctx.guild.id} 清理同步完成，共 {len(synced)} 个命令。")

    except Exception as e:
        # 如果出错，直接发新消息报错，不用 edit 了
        await ctx.send(f"❌ **清理过程中断**\n错误详情: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    bot.run(TOKEN)
