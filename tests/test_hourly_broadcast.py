import asyncio
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from config import HOURLY_BROADCAST_CHANNEL_ID as CHANNEL_ID, HOURLY_BROADCAST_GUILD_ID as GUILD_ID, TZ_SHANGHAI
from cogs.broadcast import db
from cogs.broadcast.cog import BroadcastCog
from cogs.broadcast.render import (
    RANDOM_RECOMMENDATION_FIELD, RECOMMENDATION_FIELD, TITLE, build_embed, completed_hour, recent_threads,
)
from cogs.broadcast.recommendations import recommendation_candidates, work_category
from cogs.core import db as core_db
from cogs.statistics import db as statistics_db


START = datetime(2026, 9, 25, 21, tzinfo=TZ_SHANGHAI)
END = START + timedelta(hours=1)


def thread(thread_id=1, created_at=START, parent_id=10):
    return SimpleNamespace(
        id=thread_id, name=f"作品 {thread_id}", parent_id=parent_id,
        guild=SimpleNamespace(id=GUILD_ID), created_at=created_at,
        jump_url=f"https://discord.com/channels/{GUILD_ID}/{thread_id}",
        owner=SimpleNamespace(display_name=f"作者 {thread_id}"), owner_id=thread_id + 100,
        applied_tags=[SimpleNamespace(name="角色卡")], flags=SimpleNamespace(pinned=False),
    )


def recommendation(thread_id=1):
    return dict(thread_id=thread_id, thread_name=f"作品 {thread_id}", forum_name="角色卡分区",
                category="角色卡", tags=["现代", "日常"], author_id=100 + thread_id, author_name=f"作者 {thread_id}")


class BroadcastRenderTests(unittest.TestCase):
    def test_completed_hour_uses_beijing_and_crosses_midnight(self):
        start, end = completed_hour(datetime(2026, 9, 25, 16, 23, tzinfo=timezone.utc))
        self.assertEqual(start, datetime(2026, 9, 25, 23, tzinfo=TZ_SHANGHAI))
        self.assertEqual(end, datetime(2026, 9, 26, 0, tzinfo=TZ_SHANGHAI))

    def test_new_threads_merge_sources_and_exclude_end_boundary_and_other_forums(self):
        cached = [dict(thread_id=1, forum_channel_id=10, thread_name="旧标题", created_at=START.isoformat()),
                  dict(thread_id=2, forum_channel_id=10, thread_name="已归档新帖", created_at=START.isoformat())]
        events = [dict(thread_id=3, forum_channel_id=10, thread_name="短时归档", created_at=START.isoformat())]
        live = [thread(1), thread(4, END), thread(5, START - timedelta(seconds=1)), thread(6, START, 11)]
        result = recent_threads(cached, events, live, {10}, START, END)
        self.assertEqual({row["thread_id"] for row in result}, {1, 2, 3})
        self.assertEqual(next(row for row in result if row["thread_id"] == 1)["thread_name"], "作品 1")

    def test_large_digest_is_short_and_does_not_include_raw_mentions_or_unbounded_titles(self):
        guild = SimpleNamespace(id=GUILD_ID, name="创作社区", icon=None)
        malicious_title = "@everyone [标题](https://example.com)\n" + "😀" * 300
        threads = [dict(thread_id=i, thread_name=malicious_title) for i in range(100)]
        updates = [dict(channel_id=i, title=malicious_title, update_message_id=1000 + i) for i in range(100)]
        pick = recommendation()
        pick["thread_name"] = malicious_title
        embed = build_embed(guild, START, END, threads, updates, [], 11, pick)
        self.assertLess(len(embed), 1800)
        self.assertEqual(len(embed.fields), 4)
        for field in embed.fields[:2]:
            self.assertEqual(field.value.count("• "), 3)
            self.assertIn("97", field.value)
            self.assertNotIn("@everyone", field.value)
            self.assertLessEqual(len(field.value.encode("utf-16-le")) // 2, 1024)
        self.assertEqual(embed.fields[2].name, RECOMMENDATION_FIELD)

    def test_empty_sections_are_hidden_and_stats_are_explicitly_cached(self):
        guild = SimpleNamespace(id=GUILD_ID, name="社区", icon=None)
        embed = build_embed(guild, START, END, [], [dict(channel_id=1, title="更新", update_message_id=2)],
                            [dict(likes=123, comments=45)], 1)
        self.assertEqual(len(embed.fields), 2)
        self.assertIn("作品更新", embed.fields[0].name)
        self.assertIn("缓存", embed.fields[1].name)
        self.assertIn("123", embed.fields[1].value)

    def test_five_recommendations_show_names_categories_tags_and_authors_within_limits(self):
        guild = SimpleNamespace(id=GUILD_ID, name="社区", icon=None)
        picks = [recommendation(i) for i in range(5)]
        embed = build_embed(guild, START, END, [], [], [], 3, random_recommendations=picks)
        self.assertEqual(embed.fields[0].name, RECOMMENDATION_FIELD)
        self.assertEqual(len(embed.fields), 7)
        for index, field in enumerate(embed.fields[1:6]):
            self.assertIn("角色卡", field.name)
            self.assertIn(f"作品 {index}", field.value)
            self.assertIn("角色卡分区", field.value)
            self.assertIn(f"<@{100 + index}>", field.value)
            self.assertIn("现代 / 日常", field.value)
            self.assertLessEqual(len(field.value.encode("utf-16-le")) // 2, 1024)
        self.assertLess(len(embed), 2000)
        again = build_embed(guild, START, END, [], [], [], 3,
                            random_recommendations=picks, daily_recommendation=False)
        self.assertEqual(again.fields[0].name, RANDOM_RECOMMENDATION_FIELD)
        self.assertNotIn(RECOMMENDATION_FIELD, [field.name for field in again.fields])

    def test_work_categories_match_my_works_but_exclude_skits(self):
        for category in ("角色卡", "预设", "美化", "工具", "世界书"):
            self.assertEqual(work_category(f"创作 · {category}", "帖子", []), category)
        self.assertEqual(work_category("综合作品", "帖子", ["工具"]), "工具")
        self.assertIsNone(work_category("角色卡 · 小剧场", "帖子", []))
        self.assertIsNone(work_category("其他", "小剧场", []))
        self.assertIsNone(work_category("闲聊", "聊天", []))

    def test_candidates_include_archived_cache_and_live_metadata_without_duplicates(self):
        forum = SimpleNamespace(id=10, name="角色卡", threads=[thread(1)])
        guild = SimpleNamespace(id=GUILD_ID)
        cached = [dict(thread_id=1, guild_id=GUILD_ID, forum_channel_id=10, thread_name="旧名", tags=[]),
                  dict(thread_id=2, guild_id=GUILD_ID, forum_channel_id=10, thread_name="归档作品", tags=[], is_archived=1),
                  dict(thread_id=3, guild_id=GUILD_ID, forum_channel_id=10, thread_name="置顶导航", tags=[], is_pinned=1),
                  dict(thread_id=4, guild_id=GUILD_ID + 1, forum_channel_id=10, thread_name="别服", tags=[])]
        result = recommendation_candidates(guild, cached, {10: forum})
        self.assertEqual({row["thread_id"] for row in result}, {1, 2})
        live = next(row for row in result if row["thread_id"] == 1)
        self.assertEqual(live["thread_name"], "作品 1")
        self.assertEqual(live["author_name"], "作者 1")


class BroadcastDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_path = core_db.DB_NAME
        self.test_path = Path(__file__).parent / f"test-broadcast-{uuid.uuid4().hex}.db"
        await core_db.close_db()
        core_db.DB_NAME = str(self.test_path)
        await core_db.init_db()
        await statistics_db.init_statistics_db()
        await db.init_db()

    async def asyncTearDown(self):
        await core_db.close_db()
        core_db.DB_NAME = self.original_path
        for suffix in ("", "-wal", "-shm"):
            Path(str(self.test_path) + suffix).unlink(missing_ok=True)

    async def test_update_window_is_half_open_and_strictly_scoped_to_guild(self):
        rows = [(GUILD_ID, START), (GUILD_ID, END), (GUILD_ID, START - timedelta(seconds=1)),
                (GUILD_ID + 1, START), (None, START), (GUILD_ID, START.astimezone(timezone.utc))]
        async with core_db.get_db() as conn:
            for index, (guild_id, timestamp) in enumerate(rows):
                await conn.execute("""INSERT INTO attachment_update_publish_log
                    (owner_id, guild_id, channel_id, protected_message_id, update_message_id, update_log, timestamp)
                    VALUES (1, ?, ?, 1, 1, '更新', ?)""", (guild_id, index, timestamp.isoformat()))
            await conn.commit()
        await db.record_thread(thread())
        await db.record_thread(thread(2, END))
        _, events, updates = await db.get_inputs(GUILD_ID, START, END)
        self.assertEqual({row["channel_id"] for row in updates}, {0, 5})
        self.assertEqual({row["thread_id"] for row in events}, {1})

    async def test_processed_and_daily_recommendation_survive_reopen(self):
        await db.mark_processed(GUILD_ID, CHANNEL_ID, END, 99, END.date().isoformat())
        await db.mark_processed(GUILD_ID, CHANNEL_ID, END)  # 不能把已发日推覆写成空记录。
        await core_db.close_db()
        await db.init_db()
        self.assertTrue(await db.is_processed(GUILD_ID, CHANNEL_ID, END))
        self.assertTrue(await db.recommendation_sent(GUILD_ID, CHANNEL_ID, END.date().isoformat()))
        self.assertFalse(await db.recommendation_sent(GUILD_ID, CHANNEL_ID, "2026-09-26"))
        self.assertFalse(await db.is_processed(GUILD_ID, CHANNEL_ID + 1, END))

    async def test_makeup_replaces_skipped_record_but_cannot_overwrite_sent_message(self):
        await db.mark_processed(GUILD_ID, CHANNEL_ID, END)
        self.assertIsNone((await db.get_record(GUILD_ID, CHANNEL_ID, END))["message_id"])
        await db.mark_processed(GUILD_ID, CHANNEL_ID, END, 99, END.date().isoformat())
        self.assertTrue(await db.recommendation_sent(GUILD_ID, CHANNEL_ID, END.date().isoformat()))
        await db.mark_processed(GUILD_ID, CHANNEL_ID, END, 100)
        record = await db.get_record(GUILD_ID, CHANNEL_ID, END)
        self.assertEqual(record["message_id"], 99)
        self.assertEqual(record["recommendation_date"], END.date().isoformat())


class BroadcastDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guild = SimpleNamespace(id=GUILD_ID, me=object(), name="社区", icon=None)
        self.channel = Mock(spec=discord.TextChannel)
        self.channel.id = CHANNEL_ID
        self.channel.guild = self.guild
        self.channel.permissions_for.return_value = SimpleNamespace(
            view_channel=True, send_messages=True, embed_links=True, read_message_history=True,
        )
        self.channel.send = AsyncMock(return_value=SimpleNamespace(id=123))
        self.bot = SimpleNamespace(get_guild=lambda _: self.guild, get_channel=lambda _: self.channel,
                                   user=SimpleNamespace(id=777))
        self.cog = BroadcastCog(self.bot)
        self.cog.find_sent_message = AsyncMock(return_value=None)
        self.embed = discord.Embed(title=TITLE, timestamp=END)
        self.cog.prepare_embed = AsyncMock(return_value=(self.embed, True))
        processed_patch = patch("cogs.broadcast.db.is_processed", AsyncMock(return_value=False))
        mark_patch = patch("cogs.broadcast.db.mark_processed", AsyncMock())
        self.processed = processed_patch.start()
        self.mark = mark_patch.start()
        self.addCleanup(processed_patch.stop)
        self.addCleanup(mark_patch.stop)
        recommendation_patch = patch("cogs.broadcast.db.recommendation_sent", AsyncMock(return_value=False))
        recommendation_patch.start()
        self.addCleanup(recommendation_patch.stop)

    async def test_sends_one_message_without_mentions_and_records_daily_pick(self):
        await self.cog.send_window(START, END)
        self.channel.send.assert_awaited_once()
        mentions = self.channel.send.call_args.kwargs["allowed_mentions"].to_dict()
        self.assertEqual(mentions["parse"], [])
        self.mark.assert_awaited_once_with(GUILD_ID, CHANNEL_ID, END, 123, END.date().isoformat())

    async def test_no_content_or_candidates_does_not_send_or_consume_daily_recommendation(self):
        self.cog.prepare_embed.return_value = None, False
        await self.cog.send_window(START, END)
        self.channel.send.assert_not_awaited()
        self.mark.assert_awaited_once_with(GUILD_ID, CHANNEL_ID, END)

    async def test_processed_hour_skips_everything(self):
        self.processed.return_value = True
        await self.cog.send_window(START, END)
        self.cog.prepare_embed.assert_not_awaited()
        self.channel.send.assert_not_awaited()

    async def test_concurrent_runs_send_only_once(self):
        async def recorded(*args):
            self.processed.return_value = True
        self.mark.side_effect = recorded
        await asyncio.gather(self.cog.send_window(START, END), self.cog.send_window(START, END))
        self.channel.send.assert_awaited_once()

    async def test_failed_send_leaves_window_retryable(self):
        self.channel.send.side_effect = RuntimeError("network failed")
        with self.assertRaisesRegex(RuntimeError, "network failed"):
            await self.cog.send_window(START, END)
        self.mark.assert_not_awaited()

    async def test_recovers_message_sent_before_database_failure_without_resending(self):
        self.cog.find_sent_message.return_value = (999, True)
        await self.cog.send_window(START, END)
        self.channel.send.assert_not_awaited()
        self.cog.prepare_embed.assert_not_awaited()
        self.mark.assert_awaited_once_with(GUILD_ID, CHANNEL_ID, END, 999, END.date().isoformat())

    async def test_missing_permissions_and_wrong_target_guild_do_not_send(self):
        self.channel.permissions_for.return_value.embed_links = False
        with self.assertRaisesRegex(RuntimeError, "权限"):
            await self.cog.send_window(START, END)
        self.channel.guild = SimpleNamespace(id=GUILD_ID + 1)
        with self.assertRaisesRegex(RuntimeError, "指定服务器"):
            await self.cog.send_window(START, END)
        self.channel.send.assert_not_awaited()
        self.mark.assert_not_awaited()

    async def test_prepare_skips_recommendation_after_daily_allowance_used(self):
        self.cog.collect = AsyncMock(return_value=([
            dict(thread_id=1, thread_name="新帖")], [], [], {10: object()}))
        self.cog.daily_report_url = AsyncMock(return_value=None)
        with patch("cogs.broadcast.db.recommendation_sent", AsyncMock(return_value=True)), \
                patch.object(self.cog, "choose_recommendations", AsyncMock()) as pool:
            embed, included = await BroadcastCog.prepare_embed(self.cog, self.guild, START, END)
        self.assertFalse(included)
        self.assertNotIn(RECOMMENDATION_FIELD, [field.name for field in embed.fields])
        pool.assert_not_awaited()

    async def test_first_active_hour_includes_one_daily_recommendation(self):
        self.cog.collect = AsyncMock(return_value=([
            dict(thread_id=1, thread_name="新帖")], [], [], {10: object()}))
        self.cog.daily_report_url = AsyncMock(return_value=None)
        with patch("cogs.broadcast.db.recommendation_sent", AsyncMock(return_value=False)), \
                patch.object(self.cog, "choose_recommendations", AsyncMock(return_value=[recommendation()])) as pool:
            embed, included = await BroadcastCog.prepare_embed(self.cog, self.guild, START, END)
            self.assertTrue(included)
            self.assertIn(RECOMMENDATION_FIELD, [field.name for field in embed.fields])
            self.assertEqual(pool.call_args.args[-1], 1)

    async def test_empty_midnight_and_later_hours_recommend_five_but_daily_allowance_only_once(self):
        self.cog.collect = AsyncMock(return_value=([], [], [], {10: object()}))
        self.cog.daily_report_url = AsyncMock(return_value=None)
        midnight = END.replace(hour=0)
        for already_sent in (False, True):
            with self.subTest(already_sent=already_sent), \
                    patch("cogs.broadcast.db.recommendation_sent", AsyncMock(return_value=already_sent)), \
                    patch.object(self.cog, "choose_recommendations", AsyncMock(return_value=[recommendation(i) for i in range(5)])) as pool:
                embed, included = await BroadcastCog.prepare_embed(self.cog, self.guild, midnight - timedelta(hours=1), midnight)
                self.assertEqual(pool.call_args.args[-1], 5)
                self.assertEqual(included, not already_sent)
                self.assertEqual(len(embed.fields), 7)
                self.assertIn(RECOMMENDATION_FIELD if not already_sent else RANDOM_RECOMMENDATION_FIELD,
                              [field.name for field in embed.fields])

    async def test_empty_candidate_pool_still_skips_without_consuming_daily_allowance(self):
        self.cog.collect = AsyncMock(return_value=([], [], [], {}))
        with patch.object(self.cog, "choose_recommendations", AsyncMock(return_value=[])):
            self.assertEqual(await BroadcastCog.prepare_embed(self.cog, self.guild, START, END), (None, False))

    async def test_random_selection_resolves_archived_posts_replaces_deleted_and_skips_pinned(self):
        self.guild.default_role = object()
        forum = SimpleNamespace(id=10, name="角色卡", guild=self.guild, threads=[],
                                permissions_for=lambda _: SimpleNamespace(view_channel=True))
        real_threads = {}
        for index in range(1, 8):
            candidate = Mock(spec=discord.Thread)
            candidate.id = index
            candidate.guild = self.guild
            candidate.parent = forum
            candidate.parent_id = forum.id
            candidate.name = f"现名 {index}"
            candidate.owner_id = 100 + index
            candidate.owner = None  # 归档帖作者不一定还在成员缓存。
            candidate.applied_tags = [SimpleNamespace(name="现代")]
            candidate.flags = SimpleNamespace(pinned=index == 2)
            candidate.is_private.return_value = False
            real_threads[index] = candidate
        self.guild.get_thread = lambda thread_id: real_threads.get(thread_id) if thread_id == 1 else None
        async def fetch(thread_id):
            if thread_id == 0:
                raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "deleted")
            return real_threads[thread_id]
        self.bot.fetch_channel = AsyncMock(side_effect=fetch)
        cached = [dict(thread_id=index, guild_id=GUILD_ID, forum_channel_id=10,
                       thread_name=f"旧名 {index}", tags=[], is_pinned=0, is_archived=1,
                       author_name=f"作者 {index}") for index in range(8)]
        with patch("cogs.broadcast.cog.random.sample", side_effect=lambda values, count: values[:count]):
            result = await self.cog.choose_recommendations(self.guild, cached, {10: forum}, 5)
        self.assertEqual([row["thread_id"] for row in result], [1, 3, 4, 5, 6])
        self.assertEqual(result[0]["thread_name"], "现名 1")
        self.assertEqual(result[0]["author_id"], 101)
        self.assertEqual(result[0]["author_name"], "作者 1")
        self.assertEqual(result[0]["tags"], ["现代"])
        self.assertEqual(result[0]["forum_name"], "角色卡")
        self.assertNotIn(1, [call.args[0] for call in self.bot.fetch_channel.await_args_list])

    async def test_recovery_matches_own_message_and_exact_window(self):
        old_embed = discord.Embed(title=TITLE, timestamp=START)
        valid_embed = discord.Embed(title=TITLE, timestamp=END)
        valid_embed.add_field(name=RECOMMENDATION_FIELD, value="作品")
        async def history(**kwargs):
            yield SimpleNamespace(id=1, author=SimpleNamespace(id=778), embeds=[valid_embed])
            yield SimpleNamespace(id=2, author=self.bot.user, embeds=[old_embed])
            yield SimpleNamespace(id=3, author=self.bot.user, embeds=[valid_embed])
        self.channel.history = history
        self.assertEqual(await BroadcastCog.find_sent_message(self.cog, self.channel, END), (3, True))

    async def test_recovery_restores_daily_pick_from_previous_window_after_restart(self):
        previous_embed = discord.Embed(title=TITLE, timestamp=START)
        previous_embed.add_field(name=RECOMMENDATION_FIELD, value="日推")
        async def history(**kwargs):
            self.assertEqual(kwargs["after"], END.replace(hour=0))
            yield SimpleNamespace(id=999, author=self.bot.user, embeds=[previous_embed])
        self.channel.history = history
        self.assertIsNone(await BroadcastCog.find_sent_message(self.cog, self.channel, END))
        self.mark.assert_awaited_once_with(GUILD_ID, CHANNEL_ID, START, 999, END.date().isoformat())

    async def test_collect_filters_hidden_forums_and_collapses_updates(self):
        public_forum = SimpleNamespace(id=10, threads=[thread()])
        hidden_forum = SimpleNamespace(id=11, threads=[thread(2, parent_id=11)])
        self.guild.forums = [public_forum, hidden_forum]
        self.guild.get_channel_or_thread = lambda _: None
        cached = [dict(thread_id=1, forum_channel_id=10, thread_name="新帖", created_at=START.isoformat()),
                  dict(thread_id=2, forum_channel_id=11, thread_name="隐藏", created_at=START.isoformat())]
        rows = [dict(channel_id=1, title="新版", update_message_id=101),
                dict(channel_id=1, title="旧版", update_message_id=100),
                dict(channel_id=2, title="隐藏更新", update_message_id=102)]
        self.bot.fetch_channel = AsyncMock(return_value=hidden_forum)
        with patch("cogs.broadcast.db.get_inputs", AsyncMock(return_value=(cached, [], rows))), \
                patch.object(self.cog, "is_source_channel", side_effect=lambda channel: channel is public_forum):
            threads, updates, stats, forums = await self.cog.collect(self.guild, START, END)
        self.assertEqual([row["thread_id"] for row in threads], [1])
        self.assertEqual([row["update_message_id"] for row in updates], [101])
        self.assertEqual(len(stats), 1)
        self.assertEqual(set(forums), {10})

    async def test_visibility_rejects_private_threads_and_other_servers(self):
        self.guild.default_role = object()
        channel = SimpleNamespace(guild=self.guild, permissions_for=lambda _: SimpleNamespace(view_channel=True))
        self.assertTrue(self.cog.is_source_channel(channel))
        channel.permissions_for = lambda role: SimpleNamespace(view_channel=role is self.guild.me)
        self.assertTrue(self.cog.is_source_channel(channel))
        channel.permissions_for = lambda role: SimpleNamespace(view_channel=False)
        self.assertFalse(self.cog.is_source_channel(channel))
        channel.guild = SimpleNamespace(id=GUILD_ID + 1)
        self.assertFalse(self.cog.is_source_channel(channel))
        private_thread = Mock(spec=discord.Thread)
        private_thread.guild = self.guild
        private_thread.is_private.return_value = True
        self.assertFalse(self.cog.is_source_channel(private_thread))

    async def test_makeup_allows_previously_skipped_hour_and_does_not_repeat_sent_hour(self):
        self.processed.return_value = True
        with patch("cogs.broadcast.db.get_record", AsyncMock(return_value=dict(message_id=None))):
            self.assertEqual(await self.cog.send_window(START, END, makeup=True), ("sent", 123))
        self.channel.send.assert_awaited_once()
        self.channel.send.reset_mock()
        with patch("cogs.broadcast.db.get_record", AsyncMock(return_value=dict(message_id=123))):
            self.assertEqual(await self.cog.send_window(START, END, makeup=True), ("already_sent", 123))
        self.channel.send.assert_not_awaited()

    async def test_makeup_command_can_target_midnight_in_fixed_channel(self):
        interaction = SimpleNamespace(
            guild_id=GUILD_ID, guild=self.guild,
            user=SimpleNamespace(guild_permissions=SimpleNamespace(administrator=True)),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        now = END.replace(hour=2, minute=10)
        with patch("cogs.broadcast.cog.datetime") as clock, \
                patch.object(self.cog, "send_window", AsyncMock(return_value=("sent", 123))) as send:
            clock.now.return_value = now
            await BroadcastCog.makeup.callback(self.cog, interaction, 结束小时=0)
            midnight = now.replace(hour=0, minute=0)
            send.assert_awaited_once_with(midnight - timedelta(hours=1), midnight, makeup=True)
            self.assertIn(str(CHANNEL_ID), interaction.followup.send.call_args.args[0])
            self.assertTrue(interaction.followup.send.call_args.kwargs["ephemeral"])
            send.reset_mock()
            await BroadcastCog.makeup.callback(self.cog, interaction, 结束小时=3)
            send.assert_not_awaited()
            interaction.user.guild_permissions.administrator = False
            await BroadcastCog.makeup.callback(self.cog, interaction)
            send.assert_not_awaited()

    async def test_late_makeup_recovery_is_not_limited_to_original_hour_message_times(self):
        recovered_embed = discord.Embed(title=TITLE, timestamp=END)
        async def history(**kwargs):
            self.assertNotIn("before", kwargs)
            yield SimpleNamespace(id=999, author=self.bot.user, embeds=[recovered_embed])
        self.channel.history = history
        self.assertEqual(await BroadcastCog.find_sent_message(self.cog, self.channel, END), (999, False))

    async def test_preview_never_sends_publicly_or_marks_processed(self):
        interaction = SimpleNamespace(
            guild_id=GUILD_ID, guild=self.guild,
            user=SimpleNamespace(guild_permissions=SimpleNamespace(administrator=True)),
            response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()),
        )
        await BroadcastCog.preview.callback(self.cog, interaction)
        self.assertTrue(interaction.followup.send.call_args.kwargs["ephemeral"])
        self.channel.send.assert_not_awaited()
        self.mark.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
