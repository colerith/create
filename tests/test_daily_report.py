import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cogs.exploration.cog import ExplorationCog
from cogs.exploration.views import DailyReportContainer


def make_threads(count, *, tagged=True):
    return [
        SimpleNamespace(
            name=f"新帖 {index}",
            owner=SimpleNamespace(display_name=f"作者 {index}"),
            parent=SimpleNamespace(name="创作分区"),
            applied_tags=[SimpleNamespace(name="角色卡")] if tagged else [],
            jump_url=f"https://discord.com/channels/1/{100 + index}",
        )
        for index in range(count)
    ]


def make_updates(count):
    return [
        dict(
            title=f"作品更新 {index}", channel_id=200 + index,
            guild_id=1, update_message_id=300 + index, owner_id=400 + index,
        )
        for index in range(count)
    ]


def walk_components(components):
    for component in components:
        yield component
        yield from walk_components(component.get("components", []))
        if "accessory" in component:
            yield from walk_components([component["accessory"]])


class DailyReportTests(unittest.IsolatedAsyncioTestCase):
    def assert_valid_payload(self, view):
        components = list(walk_components(view.to_components()))
        self.assertLessEqual(len(components), 40)
        return components

    async def test_both_paginated_lists_with_tags_fit_component_limit(self):
        threads = make_threads(9)
        view = DailyReportContainer(threads, make_updates(11), "今日日报", None)
        components = self.assert_valid_payload(view)
        text = "\n".join(item.get("content", "") for item in components)
        for thread in threads[:4]:
            self.assertIn(thread.name, text)
            self.assertIn(thread.owner.display_name, text)
            self.assertIn(thread.jump_url, [item.get("url") for item in components])
        self.assertIn("角色卡", text)
        self.assertIn("创作分区", text)
        for index in range(5):
            self.assertIn(f"作品更新 {index}", text)
        self.assertFalse(view.btn_thread_next.disabled)
        self.assertFalse(view.btn_update_next.disabled)

    async def test_empty_single_and_multiple_pages_stay_within_limit(self):
        for tagged in (False, True):
            for thread_count in (0, 1, 4, 5, 9):
                for update_count in (0, 1, 5, 6, 11):
                    with self.subTest(tagged=tagged, threads=thread_count, updates=update_count):
                        view = DailyReportContainer(
                            make_threads(thread_count, tagged=tagged),
                            make_updates(update_count), "日报", None,
                        )
                        for thread_page in range(view.thread_total_pages):
                            for update_page in range(view.update_total_pages):
                                view.thread_page = thread_page
                                view.update_page = update_page
                                view.update_container()
                                self.assert_valid_payload(view)

    async def test_pagination_preserves_all_entries_and_independent_positions(self):
        threads = make_threads(9)
        updates = make_updates(11)
        view = DailyReportContainer(threads, updates, "日报", None)
        interaction = SimpleNamespace(response=SimpleNamespace(edit_message=AsyncMock()))
        seen_urls = set()
        seen_text = []
        for page in range(view.thread_total_pages):
            components = self.assert_valid_payload(view)
            seen_urls.update(item.get("url") for item in components)
            self.assertEqual(view.update_page, 0)
            if page < view.thread_total_pages - 1:
                await view.on_thread_next(interaction)
        self.assertTrue(view.btn_thread_next.disabled)
        for page in range(view.update_total_pages):
            components = self.assert_valid_payload(view)
            seen_text.extend(item.get("content", "") for item in components)
            self.assertEqual(view.thread_page, view.thread_total_pages - 1)
            if page < view.update_total_pages - 1:
                await view.on_update_next(interaction)
        self.assertTrue(view.btn_update_next.disabled)
        self.assertTrue({thread.jump_url for thread in threads} <= seen_urls)
        for row in updates:
            self.assertIn(f"**{row['title']}**", "\n".join(seen_text))
        for _ in range(view.thread_total_pages):
            await view.on_thread_prev(interaction)
        for _ in range(view.update_total_pages):
            await view.on_update_prev(interaction)
        self.assertEqual((view.thread_page, view.update_page), (0, 0))
        self.assertTrue(view.btn_thread_prev.disabled)
        self.assertTrue(view.btn_update_prev.disabled)
        self.assert_valid_payload(view)
        interaction.response.edit_message.assert_awaited_with(view=view)


class PublicPanelRebuildTests(unittest.IsolatedAsyncioTestCase):
    async def test_build_failure_keeps_existing_panels_and_records(self):
        for failed_builder in ("_build_daily_report_view", "_build_daily_recommend_view"):
            with self.subTest(builder=failed_builder):
                cog = object.__new__(ExplorationCog)
                cog.bot = SimpleNamespace(user=SimpleNamespace(
                    display_avatar=SimpleNamespace(url="https://cdn.discordapp.com/embed/avatars/0.png")
                ))
                cog._build_daily_report_view = AsyncMock()
                cog._build_daily_recommend_view = AsyncMock()
                getattr(cog, failed_builder).side_effect = ValueError("invalid panel")
                cog._delete_message_if_exists = AsyncMock()
                cog._cleanup_untracked_public_panels = AsyncMock()
                channel = SimpleNamespace(id=123, send=AsyncMock())
                with (
                    patch("cogs.exploration.cog.get_panel_message_id", AsyncMock(return_value=1)),
                    patch("cogs.exploration.cog.recommend_db.get_panel_message_id", AsyncMock(return_value=2)),
                    patch("cogs.exploration.cog.remove_panel_record", AsyncMock()) as remove,
                    patch("cogs.exploration.cog.recommend_db.remove_panel_message", AsyncMock()) as remove_recommend,
                    patch("cogs.exploration.cog.set_panel_message_id", AsyncMock()),
                    patch("cogs.exploration.cog.asyncio.sleep", AsyncMock()),
                ):
                    with self.assertRaisesRegex(ValueError, "invalid panel"):
                        await cog.rebuild_ordered_public_panels(channel, include_recommend=True)
                    cog._delete_message_if_exists.assert_not_awaited()
                    cog._cleanup_untracked_public_panels.assert_not_awaited()
                    remove.assert_not_awaited()
                    remove_recommend.assert_not_awaited()
                    channel.send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
