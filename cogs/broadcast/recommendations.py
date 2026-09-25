"""按“我的作品”的关键词分类，合并缓存和实时帖子作为推荐候选。"""

from config import RECOMMEND_TARGET_KEYWORDS


def work_category(forum_name, title, tags):
    if "小剧场" in forum_name:
        return None
    haystack = " ".join([forum_name, title, " ".join(tags)])
    category = next((keyword for keyword in RECOMMEND_TARGET_KEYWORDS if keyword in haystack), None)
    return category if category != "小剧场" else None


def thread_details(thread, forum, cached=None):
    cached = cached or {}
    owner = thread.owner
    return {
        "thread_id": thread.id,
        "guild_id": thread.guild.id,
        "forum_channel_id": forum.id,
        "thread_name": thread.name,
        "tags": [tag.name for tag in thread.applied_tags],
        "author_id": thread.owner_id or cached.get("author_id"),
        "author_name": owner.display_name if owner else cached.get("author_name"),
        "is_pinned": thread.flags.pinned,
    }


def recommendation_candidates(guild, cached, forums):
    rows = {row["thread_id"]: dict(row) for row in cached
            if row["forum_channel_id"] in forums and row.get("guild_id", guild.id) == guild.id}
    for forum in forums.values():
        for thread in forum.threads:
            rows[thread.id] = thread_details(thread, forum, rows.get(thread.id))
    candidates = []
    for row in rows.values():
        forum = forums[row["forum_channel_id"]]
        category = work_category(forum.name, row["thread_name"], row.get("tags") or [])
        if category is None or row.get("is_pinned"):
            continue
        row["category"] = category
        row["forum_name"] = forum.name
        candidates.append(row)
    return candidates
