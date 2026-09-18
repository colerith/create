import asyncio
import json
import uuid
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cogs.core import db as core_db
from cogs.protection.views import (
    DraftUpdateLogsView, ProtectionDraftView,
    build_reusable_metadata_from_row, split_update_content,
)


class UpdateLogsTests(unittest.IsolatedAsyncioTestCase):
    def test_long_content_is_lossless_and_within_discord_limit(self):
        for text in ['文' * 4000, '😀' * 4000, '第一行\n' * 1800]:
            chunks = list(split_update_content(text))
            self.assertEqual(''.join(chunks), text)
            self.assertTrue(all(len(c.encode('utf-16-le')) // 2 <= 2000 for c in chunks))

    async def test_manager_rebuild_and_empty_state(self):
        draft = ProtectionDraftView(None, SimpleNamespace(id=1, display_name='测试'), [])
        view = DraftUpdateLogsView(draft)
        for count in [1, 5, 25, 0]:
            draft.draft_update_logs = [{'text': '日志', 'attachments': []} for _ in range(count)]
            view.rebuild()
            view.to_components()
            self.assertEqual(sum(type(item).__name__ == 'Select' for item in view._layout_items), 1)
            self.assertEqual(view.add_log.disabled, count == 25)
            self.assertEqual(view.edit_log.disabled, count == 0)

    async def test_database_migration_and_reuse_all_logs(self):
        previous = core_db.DB_NAME
        test_path = Path(__file__).resolve().parent / f"test-{uuid.uuid4().hex}.db"
        core_db.DB_NAME = str(test_path)
        try:
            await core_db.init_db()
            await core_db.init_db()
            entries = [{'text': '第一条', 'message_ids': [11, 12]}, {'text': '第二条', 'message_ids': [21]}]
            async with core_db.get_db() as db:
                await db.execute('INSERT INTO protected_update_logs VALUES (?, ?)', (1, json.dumps(entries)))
                await db.commit()
            attachment = SimpleNamespace(filename='test.txt', read=AsyncMock(return_value=b'data'), is_spoiler=lambda: False)
            channel = SimpleNamespace(fetch_message=AsyncMock(return_value=SimpleNamespace(attachments=[attachment])))
            bot = SimpleNamespace(get_channel=lambda _: channel)
            row = dict(message_id=1, channel_id=2, title='标题', log='作者提示', update_log='旧摘要', unlock_type='like', mention_users=1, password=None)
            metadata = await build_reusable_metadata_from_row(bot, row)
            self.assertEqual([e['text'] for e in metadata['draft_update_logs']], ['第一条', '第二条'])
            self.assertEqual(len(metadata['draft_update_attachments']), 2)
            self.assertTrue(metadata['mention_users'])
            self.assertEqual(channel.fetch_message.await_count, 2)
            draft = ProtectionDraftView(bot, SimpleNamespace(id=1, display_name='测试'), [], draft_defaults=metadata)
            self.assertEqual(len(draft.draft_update_logs), 2)
            self.assertTrue(draft.mention_users)
            draft.user.display_avatar = SimpleNamespace(url="https://example.com/avatar.png")
            bot.user = SimpleNamespace(display_avatar=draft.user.display_avatar)
            bot.get_cog = lambda _: None
            draft.draft_update_logs[0]["text"] = "😀" * 4000
            draft.draft_update_logs[1]["text"] = "第二条 @everyone"
            sent = []
            async def send(*args, **kwargs):
                sent.append((args, kwargs))
                return SimpleNamespace(id=100 + len(sent), channel=SimpleNamespace(id=2), pin=AsyncMock())
            interaction = SimpleNamespace(channel=SimpleNamespace(id=2, send=send), guild=None, followup=SimpleNamespace(send=AsyncMock()))
            with patch('cogs.protection.views.build_storage_entries_from_attachments', AsyncMock(return_value=[])):
                await draft.publish(interaction)
            updates = [(args[0], kwargs) for args, kwargs in sent if args]
            self.assertGreater(len(updates), 2)
            self.assertTrue(updates[0][1]["allowed_mentions"].everyone)
            self.assertTrue(all(not kwargs["allowed_mentions"].everyone for _, kwargs in updates[1:]))
            self.assertEqual(sum(len(kwargs['files']) for _, kwargs in updates), 2)
            self.assertTrue(all(len(text.encode('utf-16-le')) // 2 <= 2000 for text, _ in updates))
            channel.fetch_message.side_effect = RuntimeError('deleted')
            metadata = await build_reusable_metadata_from_row(bot, row)
            self.assertEqual(len(metadata['draft_update_logs']), 2)
            self.assertIn('update_attachment_reuse_warning', metadata)
        finally:
            await core_db.close_db()
            core_db.DB_NAME = previous
            for suffix in ["", "-wal", "-shm"]:
                Path(str(test_path) + suffix).unlink(missing_ok=True)


if __name__ == '__main__':
    unittest.main()

