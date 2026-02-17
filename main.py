# main.py

import discord
from discord.ext import commands
import os
from dotenv import load_dotenv
import aiohttp
import asyncio
import traceback

from config import TOKEN, TEST_GUILDS

if not TOKEN:
    print("错误：在 .env (或 config.py) 文件中找不到 DISCORD_TOKEN。")
    exit()

class ChimidanBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self.http_session: aiohttp.ClientSession = None

    async def setup_hook(self):
        """机器人启动时运行的钩子函数"""
        self.http_session = aiohttp.ClientSession()

        from core.db import init_db
        await init_db()
        print("✅ 数据库检查完毕。")

        print("--- 正在加载插件 ---")
        loaded_cogs = 0
        for cog_folder in os.listdir('./cogs'):
            # 忽略非目录和以 '__' 开头的特殊目录
            if not os.path.isdir(f'./cogs/{cog_folder}') or cog_folder.startswith('__'):
                continue

            try:
                await self.load_extension(f'cogs.{cog_folder}.cog')
                print(f"📦 加载插件成功: cogs.{cog_folder}.cog")
                loaded_cogs += 1
            except Exception as e:
                print(f"❌ 加载插件失败 cogs.{cog_folder}.cog. 错误: {e}")

        print(f"--- 共加载 {loaded_cogs} 个插件 ---")

        commands_list = self.tree.get_commands()
        print(f"📊 [诊断] Bot 内存中已加载 {len(commands_list)} 个斜杠命令: {[cmd.name for cmd in commands_list]}")

    async def close(self):
        """机器人关闭时运行"""
        await super().close()
        if self.http_session:
            await self.http_session.close()

# 实例化机器人
bot = ChimidanBot()

# --- 事件监听 ---
@bot.event
async def on_ready():
    """当机器人准备就绪时触发"""
    print(f'Logged in as {bot.user} (ID: {bot.user.id})')
    print('------')

# --- 手动急救同步命令 (这个命令非常有用，必须保留！) ---
@bot.command(name="forcesync")
@commands.is_owner()
async def forcesync(ctx: commands.Context, guild_id: int = None):
    """[Owner] 手动强制同步命令到指定服务器，或当前服务器"""
    target_guild_id = guild_id or ctx.guild.id
    target_guild = discord.Object(id=target_guild_id)
    if not target_guild:
        return await ctx.send("找不到目标服务器。")

    msg = await ctx.send(f"🚑 正在执行手动同步到服务器: **{target_guild_id}**...")

    try:
        bot.tree.copy_global_to(guild=target_guild)
        synced = await bot.tree.sync(guild=target_guild)

        await msg.edit(content=f"✅ **同步完成！**\n已在服务器 `{target_guild_id}` 注册了 **{len(synced)}** 个命令。\n请**重启 Discord 客户端** (Ctrl+R) 刷新缓存。")
        print(f"Manual sync to {target_guild_id}: {len(synced)} cmds.")

    except Exception as e:
        await msg.edit(content=f"❌ 同步失败: {e}")
        traceback.print_exc()


# --- 启动 ---
if __name__ == "__main__":
    try:
        bot.run(TOKEN)
    except discord.LoginFailure:
        print("❌ 登录失败：无效的 Discord Token。请检查你的 .env 或 config.py 文件。")
    except Exception as e:
        print(f"启动过程中发生未知错误: {e}")