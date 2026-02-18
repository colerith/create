# cogs/recommend/__init__.py

from discord.ext import commands
from .cog import RecommendCog

async def setup(bot: commands.Bot):
    """Cog 的加载入口"""
    await bot.add_cog(RecommendCog(bot))
    print("✅ [RecommendCog] 已成功加载。")