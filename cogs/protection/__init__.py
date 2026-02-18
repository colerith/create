# cogs/protection/__init__.py

from discord.ext import commands
from .cog import ProtectionCog 
async def setup(bot: commands.Bot):
    """
    Cog的加载入口。

    当机器人加载这个扩展时，会自动调用此函数。
    """
    await bot.add_cog(ProtectionCog(bot))
    print("✅ [ProtectionCog] 已成功加载。")
