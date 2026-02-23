from discord.ext import commands
from .cog import StatisticsCog
from . import db as statistics_db # 导入db模块

async def setup(bot: commands.Bot):
    """Cog的加载入口"""
    await statistics_db.init_statistics_db()
    await bot.add_cog(StatisticsCog(bot))
