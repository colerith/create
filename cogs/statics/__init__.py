from discord.ext import commands
from .cog import StatisticsCog

async def setup(bot: commands.Bot):
    """Cog 的加载入口"""
    await bot.add_cog(StatisticsCog(bot))
    print("✅ [StatisticsCog] 已成功加载。")