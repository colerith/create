#cogs/recommend/__init__.py

from .cog import RecommendCog

async def setup(bot):
    await bot.add_cog(RecommendCog(bot))