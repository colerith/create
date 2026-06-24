from discord.ext import commands

from .cog import CoreBackupCog


async def setup(bot: commands.Bot):
    await bot.add_cog(CoreBackupCog(bot))
    print("✅ [CoreBackupCog] 已成功加载。")
