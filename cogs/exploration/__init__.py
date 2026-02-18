# cogs/exploration/__init__.py

from discord.ext import commands
from .cog import ExplorationCog

async def setup(bot: commands.Bot):
    """Cog的加载入口"""
    await bot.add_cog(ExplorationCog(bot))
    print("✅ [ExplorationCog] 已成功加载。")
