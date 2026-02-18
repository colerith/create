# cogs/recommend/utils.py

import discord
from config import RECOMMEND_TARGET_KEYWORDS

def get_card_forums(guild: discord.Guild) -> list[discord.ForumChannel]:
    """获取所有包含目标关键词的论坛频道"""
    return [c for c in guild.forums if any(keyword in c.name for keyword in RECOMMEND_TARGET_KEYWORDS)]

async def get_random_thread_pool(guild: discord.Guild, specific_channel_id: int = None) -> list[discord.Thread]:
    """获取符合条件的帖子池 (排除置顶帖)"""
    forums = get_card_forums(guild)
    if specific_channel_id:
        forums = [f for f in forums if f.id == specific_channel_id]

    return [t for forum in forums for t in forum.threads if not t.flags.pinned]

async def fetch_thread_details(thread: discord.Thread) -> dict:
    """获取帖子的详细信息"""
    try:
        starter = thread.starter_message or (await thread.history(limit=1, oldest_first=True).flatten())[0]
    except (discord.errors.Forbidden, IndexError):
        starter = None

    intro, image_url = "（暂无介绍）", None
    if starter:
        # 简介处理
        intro = starter.content[:300] + "..." if len(starter.content) > 300 else starter.content
        # 优先从附件获取图片
        image_url = next((att.url for att in starter.attachments if att.content_type and "image" in att.content_type), None)

    tags = [tag.name for tag in thread.applied_tags] or ["无标签"]
    owner = thread.owner

    return {
        "title": thread.name,
        "author_name": owner.display_name if owner else "未知作者",
        "author_mention": owner.mention if owner else "未知作者",
        "author_avatar": owner.display_avatar.url if owner else None,
        "intro": intro,
        "category": thread.parent.name,
        "tags": tags,
        "url": thread.jump_url,
        "image": image_url
    }