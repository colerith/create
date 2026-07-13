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

    starter = None
    try:
        if thread.starter_message:
            starter = thread.starter_message
        else:
            messages = [message async for message in thread.history(limit=1, oldest_first=True)]
            if messages:
                starter = messages[0]

    except (discord.errors.Forbidden, IndexError, Exception) as e:
        print(f"Error fetching thread details for {thread.id}: {e}")
        starter = None

    intro = "（暂无介绍）"
    image_url = None

    if starter:
        # 简介处理 (截取前300字)
        if starter.content:
            intro = starter.content[:300] + "..." if len(starter.content) > 300 else starter.content

        # 优先从附件获取图片
        if starter.attachments:
            image_url = next((att.url for att in starter.attachments if att.content_type and "image" in att.content_type), None)

    # 标签处理
    tags = [tag.name for tag in thread.applied_tags] if thread.applied_tags else ["无标签"]

    # 作者处理
    owner = thread.owner

    return {
        "title": thread.name,
        "author_name": owner.display_name if owner else "未知作者",
        "author_mention": f"@{owner.display_name}" if owner else "未知作者",
        "author_avatar": owner.display_avatar.url if owner else None,
        "intro": intro,
        "category": thread.parent.name if thread.parent else "未知分区",
        "tags": tags,
        "url": thread.jump_url,
        "image": image_url
    }
