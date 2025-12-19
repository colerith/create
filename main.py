import discord
from discord.ext import commands
import os
from dotenv import load_dotenv
import aiohttp

from cogs.protection import DownloadView
from database import init_db

load_dotenv()

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
        super().__init__(command_prefix="!", intents=intents, help_command=None)
        
        self.http_session: aiohttp.ClientSession = None

    async def setup_hook(self):
        self.http_session = aiohttp.ClientSession()

        await init_db()

        for filename in os.listdir('./cogs'):
            if filename.endswith('.py'):
                await self.load_extension(f'cogs.{filename[:-3]}')

        self.add_view(DownloadView(self)) 
        
        await self.tree.sync()
        print(f"奇米蛋已上线来捉！登录为：{self.user}")

    async def close(self):
        await super().close()
        if self.http_session:
            await self.http_session.close()

bot = ChimidanBot()

if __name__ == "__main__":
    bot.run(TOKEN) 