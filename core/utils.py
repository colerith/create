# /core/utils.py

import discord
from config import TARGET_KEYWORDS  # 1. 从正确的配置文件导入常量

def get_card_forums(guild: discord.Guild):
    """
    获取所有包含目标关键词的论坛频道。
    这个函数依赖于 config.py 中定义的 TARGET_KEYWORDS。
    """
    return [c for c in guild.forums if any(keyword in c.name for keyword in TARGET_KEYWORDS)]
