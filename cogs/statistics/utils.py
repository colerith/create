# cogs/statistics/utils.py

import discord
import asyncio
from datetime import datetime, timedelta

from config import TZ_SHANGHAI

async def fetch_forum_stats(forum: discord.ForumChannel) -> dict:
    """获取一个论坛频道的综合统计数据"""

    # --- 初始化统计变量 ---
    total_posts = 0
    recent_posts_count = 0 # 7天内新增
    all_threads_with_stats = []

    # --- 数据抓取 ---
    # 【修复】现在传入的 forum 对象是完整的 ForumChannel，可以安全地访问 .threads
    threads_to_scan = forum.threads.copy() # 使用 .copy() 避免修改原始列表
    try:
        async for thread in forum.archived_threads(limit=1000):
            threads_to_scan.append(thread)
    except discord.Forbidden:
        print(f"权限不足，无法获取 {forum.name} 的归档帖子进行统计。")
    except Exception as e:
        print(f"获取 {forum.name} 归档帖子时出错: {e}")

    total_posts = len(threads_to_scan)

    # 计算7天内新增
    seven_days_ago = datetime.now(TZ_SHANGHAI) - timedelta(days=7)
    for thread in threads_to_scan:
        if thread.created_at.astimezone(TZ_SHANGHAI) >= seven_days_ago:
            recent_posts_count += 1

    # --- 异步处理每个帖子 ---
    sem = asyncio.Semaphore(10)

    async def process_thread(thread: discord.Thread):
        try:
            async with sem:
                starter_message = thread.starter_message
                if not starter_message:
                    try:
                        # 【修复】正确地从异步生成器中获取消息
                        history = thread.history(limit=1, oldest_first=True)
                        starter_message = await history.__anext__()
                    except (StopAsyncIteration, IndexError, discord.Forbidden):
                        return None # 获取不到起始消息

                likes = 0
                if starter_message.reactions:
                    for reaction in starter_message.reactions:
                        likes += reaction.count

                comments = thread.message_count - 1 if thread.message_count else 0
                if comments < 0:
                    comments = 0

                return {
                    "thread": thread,
                    "likes": likes,
                    "comments": comments
                }
        except Exception:
            return None

    tasks = [process_thread(thread) for thread in threads_to_scan]
    results = await asyncio.gather(*tasks)
    all_threads_with_stats = [res for res in results if res is not None]

    # --- 数据排序 ---
    sorted_by_likes = sorted(all_threads_with_stats, key=lambda x: x['likes'], reverse=True)

    # --- 最终返回的数据结构 ---
    return {
        "total_posts": total_posts,
        "recent_posts_count": recent_posts_count,
        "hottest_posts": sorted_by_likes[:5],
        "coldest_posts": sorted_by_likes[-5:][::-1]
    }
