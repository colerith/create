#cogs/exploration/__init__.py

from cog import ExplorationCog

async def setup(bot):
    await bot.add_cog(ExplorationCog(bot))