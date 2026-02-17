import discord

def get_card_forums(guild: discord.Guild):
    """【修改】获取所有包含目标关键词的论坛频道"""
    TARGET_KEYWORDS = ["角色卡", "预设", "美化", "工具", "小剧场", "世界书"]
    return [c for c in guild.forums if any(keyword in c.name for keyword in TARGET_KEYWORDS)]