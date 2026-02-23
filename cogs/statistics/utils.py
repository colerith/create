# cogs/statistics/utils.py

import discord
from datetime import datetime, timedelta

async def fetch_forum_stats(forum: discord.ForumChannel) -> dict:
    """
    获取并分析单个论坛频道的数据。
    """
    all_threads = forum.threads
    archived_threads = await forum.archived_threads(limit=None).flatten()
    all_threads.extend(archived_threads)

    # 过滤掉没有起始消息或权限问题的帖子
    valid_threads = []
    for t in all_threads:
        try:
            # 尝试获取起始消息，这是判断活跃度的关键
            starter_message = t.starter_message or (await t.history(limit=1, oldest_first=True).flatten())[0]
            if starter_message:
                valid_threads.append((t, starter_message))
        except (discord.errors.Forbidden, IndexError):
            continue # 跳过无法访问或空的帖子

    # 1. 计算总数和近期数量
    now = datetime.utcnow()
    seven_days_ago = now - timedelta(days=7)
    recent_threads_count = sum(1 for t, _ in valid_threads if t.created_at.replace(tzinfo=None) > seven_days_ago)

    # 2. 按点赞数（回应数）排序
    # 回应数通常更能反映社区互动
    sorted_by_reactions = sorted(
        [
            {
                "thread": t,
                "first_message": sm,
                "reaction_count": len(sm.reactions) if sm.reactions else 0, # 计算所有回应的数量
            }
            for t, sm in valid_threads
        ],
        key=lambda x: x['reaction_count'],
        reverse=True
    )

    # 正确地获取热门和冷门帖子
    hottest_posts = sorted_by_reactions[:5]
    coldest_posts = sorted_by_reactions[-5:][::-1] # 取最后5个并反转顺序

    return {
        "total_threads": len(valid_threads),
        "recent_threads_count": recent_threads_count,
        "hottest_posts": hottest_posts,
        "coldest_posts": coldest_posts,
    }