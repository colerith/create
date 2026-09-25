from datetime import datetime, timedelta
import random

import discord

from config import TZ_SHANGHAI


TITLE = "📣 创作之蛋 · 整点速递"
RECOMMENDATION_FIELD = "🌟 今日推荐"
MAX_ITEMS = 3

# 沿用统计里程碑里的「奇米蛋 / 小喇叭 / 香香作品蛋」口吻，开场保持简短。
INTRO_LINES = {
    "both": (
        "奇米蛋抱着小喇叭滚来啦！这一小时收好 **{new}** 篇新帖、**{updates}** 份作品更新，香香创作请查收捏～",
        "叮咚——**{new}** 颗新作品蛋出炉，**{updates}** 份更新也装进小篮子啦，快来挑挑喜欢的捏！",
        "报告报告，奇米蛋捡到 **{new}** 篇新帖和 **{updates}** 份更新！这一小时的创作小篮子装好咯～",
    ),
    "threads": (
        "奇米蛋闻到新作品的香味啦！这一小时孵出 **{new}** 篇新帖，抱进小篮子送来给你捏～",
        "啵！**{new}** 颗新作品蛋冒头啦，奇米蛋抱着小喇叭来报喜咯～",
    ),
    "updates": (
        "奇米蛋滚来递更新啦！这一小时有 **{updates}** 份作品更新，快去看看作者添了什么新惊喜捏～",
        "叮咚——收到 **{updates}** 份作品更新！奇米蛋已经收进小篮子，戳开就能看啦～",
    ),
}


def completed_hour(now):
    end = now.astimezone(TZ_SHANGHAI).replace(minute=0, second=0, microsecond=0)
    return end - timedelta(hours=1), end


def parse_time(value):
    if not value:
        return None
    try:
        value = datetime.fromisoformat(value) if isinstance(value, str) else value
    except ValueError:
        return None
    # 兼容早期数据库中未带偏移量的北京时间。
    return value.replace(tzinfo=TZ_SHANGHAI) if value.tzinfo is None else value


def recent_threads(cached, events, live_threads, forum_ids, start, end):
    rows = {row["thread_id"]: dict(row) for row in [*cached, *events]
            if row["forum_channel_id"] in forum_ids}
    for thread in live_threads:
        if thread.parent_id in forum_ids:
            rows[thread.id] = dict(thread_id=thread.id, thread_name=thread.name,
                                   forum_channel_id=thread.parent_id, created_at=thread.created_at)
    result = [row for row in rows.values()
              if (created := parse_time(row.get("created_at"))) and start <= created < end]
    return sorted(result, key=lambda row: (parse_time(row["created_at"]), row["thread_id"]), reverse=True)


def safe_label(value, limit=42):
    text = " ".join(str(value or "未命名作品").split())
    text = text[:limit - 1] + "…" if len(text) > limit else text
    text = discord.utils.escape_mentions(discord.utils.escape_markdown(text))
    # 方括号和反斜线不能改变 Markdown 链接的标签边界。
    return text.replace("[", "\\[").replace("]", "\\]")


def build_embed(guild, start, end, threads, updates, cached, forum_count,
                recommendation=None, daily_url=None):
    intro_kind = "both" if threads and updates else "threads" if threads else "updates"
    description = random.choice(INTRO_LINES[intro_kind]).format(new=len(threads), updates=len(updates))
    embed = discord.Embed(
        title=TITLE,
        description=description,
        colour=0xA7B8EF,
        timestamp=end,
    )
    embed.set_author(name=safe_label(guild.name, 60))
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    def add_entries(name, rows, title_key, id_key, message_key=None):
        if not rows:
            return
        lines = []
        for row in rows[:MAX_ITEMS]:
            url = f"https://discord.com/channels/{guild.id}/{row[id_key]}"
            if message_key:
                url += f"/{row[message_key]}"
            lines.append(f"• [{safe_label(row[title_key])}]({url})")
        if len(rows) > MAX_ITEMS:
            lines.append(f"还有 **{len(rows) - MAX_ITEMS}** 条收在完整日报里捏～" if daily_url
                         else f"还有 **{len(rows) - MAX_ITEMS}** 条，去论坛继续翻翻捏～")
        embed.add_field(name=f"{name} · {len(rows)}", value="\n".join(lines), inline=False)

    add_entries("🌱 新帖", threads, "thread_name", "thread_id")
    add_entries("✨ 作品更新", updates, "title", "channel_id", "update_message_id")
    if recommendation:
        embed.add_field(
            name=RECOMMENDATION_FIELD,
            value=("奇米蛋今天捧来这颗宝藏作品蛋，戳开看看捏～\n"
                   f"• [{safe_label(recommendation.name)}]({recommendation.jump_url})"),
            inline=False,
        )
    likes = sum(max(0, row.get("likes") or 0) for row in cached)
    comments = sum(max(0, row.get("comments") or 0) for row in cached)
    stats = f"{forum_count} 个分区 · 收录 {len(cached):,} 帖 · 累计 {likes:,} 赞 / {comments:,} 评论"
    if not cached:
        stats = f"{forum_count} 个分区 · 帖子统计缓存正在准备中"
    if daily_url:
        stats += f"\n[查看完整日报]({daily_url})"
    embed.add_field(name="📊 频道速览（缓存统计）", value=stats, inline=False)
    embed.set_footer(text=f"{start:%m/%d %H:%M}–{end:%m/%d %H:%M} · 北京时间 · 每小时汇总")
    return embed
