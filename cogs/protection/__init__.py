from cog import ProtectionCog

async def setup(bot):
    await bot.add_cog(ProtectionCog(bot))