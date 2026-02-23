import discord
from datetime import datetime, timedelta
import asyncio
from config import TZ_SHANGHAI

async def fetch_forum_stats(forum: discord.ForumChannel) -> dict:
    """
    获取单个论坛频道的统计数据。

    返回一个字典，包含:
    - total_count: 帖子总数
    - weekly_count: 近7日新增帖子数
    - hot_threads: 回复最多的前5个帖子
    - cold_gems: 创建超过14天且回复数少于3的随机5个帖子
    """
    if not forum:
        return {}

    # 为了效率，我们尽可能一次性获取所有帖子
    # 注意: 如果论坛帖子数万，guild.fetch_threads() 可能更优，但 forum.threads 对于大多数场景足够
    all_threads = forum.threads
    if not all_threads:
        return {
            'total_count': 0,
            'weekly_count': 0,
            'hot_threads': [],
            'cold_gems': [],
        }

    now = datetime.now(TZ_SHANGHAI)
    seven_days_ago = now - timedelta(days=7)
    fourteen_days_ago = now - timedelta(days=14)

    weekly_threads = []

    # Python 3.12+ 可以用 aiter/anext，但为了兼容性，普通 for 循环更安全
    for thread in all_threads:
        if thread.created_at.astimezone(TZ_SHANGHAI) > seven_days_ago:
            weekly_threads.append(thread)

    # 统计热门帖子 (按消息数排序)
    # starter_message 不计入 message_count，所以这个值约等于回复数
    hot_threads = sorted(all_threads, key=lambda t: t.message_count, reverse=True)[:3]

    # 统计冷门好帖
    # 定义：创建超过14天，且回复数少于3 (message_count < 3)
    potential_gems = [
        t for t in all_threads
        if t.created_at.astimezone(TZ_SHANGHAI) < fourteen_days_ago and t.message_count < 3
    ]

    # 随机选3个，如果不够就全选
    import random
    cold_gems = random.sample(potential_gems, min(len(potential_gems), 5))

    return {
        'total_count': len(all_threads),
        'weekly_count': len(weekly_threads),
        'hot_threads': hot_threads,
        'cold_gems': cold_gems,
    }
