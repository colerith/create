from __future__ import annotations

import discord


TARGET_FORUM_CHANNEL_ID = 1394925639026606171
TARGET_CATEGORY_ID = 1396080555266670652
TARGET_ROLE_ID = 1509535887300493322
MIN_LIKES_FOR_ROLE = 20
SCAN_INTERVAL_HOURS = 6


def get_target_forums(guild: discord.Guild) -> list[discord.ForumChannel]:
    """返回需要参与点赞身份组扫描的论坛频道。"""
    forums_by_id: dict[int, discord.ForumChannel] = {}

    direct_forum = guild.get_channel(TARGET_FORUM_CHANNEL_ID)
    if isinstance(direct_forum, discord.ForumChannel):
        forums_by_id[direct_forum.id] = direct_forum

    category = guild.get_channel(TARGET_CATEGORY_ID)
    if isinstance(category, discord.CategoryChannel):
        for channel in category.channels:
            if isinstance(channel, discord.ForumChannel):
                forums_by_id[channel.id] = channel

    return list(forums_by_id.values())
