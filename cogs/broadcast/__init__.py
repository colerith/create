from .cog import BroadcastCog


async def setup(bot):
    await bot.add_cog(BroadcastCog(bot))
