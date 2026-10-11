"""Offline tests for the persistent per-guild changelog embed."""
import asyncio
import os
import sys
import tempfile
import unittest
from unittest import mock

import discord

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
import guild_settings
import release_announcements
from release_notes import RELEASE_BOARD_TITLE, ReleaseAnnouncement


RELEASES = (
    ReleaseAnnouncement("2026-09-02", "current club book", ("Ask what we are reading.",)),
    ReleaseAnnouncement("2026-08-31", "nominate", ("Nominate from a button.",)),
)


class FakeBoardMessage:
    def __init__(self, message_id, channel):
        self.id = message_id
        self.channel = channel
        self.edits = []
        self.deleted = False
        self.embeds = []
        self.author = mock.Mock(bot=True)

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        if kwargs.get("embed") is not None:
            self.embeds = [kwargs["embed"]]

    async def delete(self):
        self.deleted = True
        self.channel.messages.pop(int(self.id), None)


class FakeBoardChannel:
    def __init__(self, channel_id, name):
        self.id = channel_id
        self.name = name
        self.messages = {}
        self.sent = []
        self.next_message_id = 9000

    async def fetch_message(self, message_id):
        message = self.messages.get(int(message_id))
        if message is None:
            raise discord.NotFound(mock.MagicMock(status=404), {"message": "Unknown Message"})
        return message

    async def history(self, *, limit=100, **kwargs):
        messages = sorted(self.messages.values(), key=lambda item: int(item.id), reverse=True)
        for message in messages[:limit]:
            yield message

    async def send(self, **kwargs):
        message = FakeBoardMessage(self.next_message_id, self)
        self.next_message_id += 1
        if kwargs.get("embed") is not None:
            message.embeds = [kwargs["embed"]]
        self.messages[message.id] = message
        self.sent.append(kwargs)
        return message

    def add_message(self, message_id, *, title=RELEASE_BOARD_TITLE, description="old"):
        message = FakeBoardMessage(message_id, self)
        message.embeds = [mock.Mock(title=title, description=description)]
        self.messages[int(message_id)] = message
        return message


class ForbiddenBoardChannel(FakeBoardChannel):
    async def fetch_message(self, message_id):
        raise discord.Forbidden(mock.MagicMock(status=403), {"message": "Missing Access"})


class HistoryForbiddenChannel(FakeBoardChannel):
    def history(self, *, limit=100, **kwargs):
        raise discord.Forbidden(mock.MagicMock(status=403), {"message": "Missing Access"})


class FakeBoardGuild:
    def __init__(self, guild_id, channels):
        self.id = int(guild_id)
        self.text_channels = list(channels)


class ReleaseBoardSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "bookclub.db")
        self.db_patch = mock.patch.object(database, "DB_PATH", self.db_path)
        self.db_patch.start()
        database.init_db()
        self.guild_id = "42"
        database.upsert_server(self.guild_id, "Club")
        guild_settings.ensure_guild_settings(self.guild_id)
        guild_settings.update_guild_settings(
            self.guild_id, release_announcements_channel="book-bot"
        )
        self.enabled = mock.patch.object(
            release_announcements.config, "RELEASE_ANNOUNCEMENTS_ENABLED", True
        )
        self.enabled.start()

    def tearDown(self):
        self.enabled.stop()
        self.db_patch.stop()
        self.tmpdir.cleanup()

    def _sync(self, *channels, announcements=RELEASES):
        guild = FakeBoardGuild(self.guild_id, channels)
        return asyncio.run(
            release_announcements.sync_release_board(guild, announcements)
        ), guild

    def test_missing_board_posts_one_silent_embed(self):
        channel = FakeBoardChannel(10, "book-bot")

        posted, _guild = self._sync(channel)

        self.assertTrue(posted)
        self.assertEqual(len(channel.sent), 1)
        self.assertNotIn("content", channel.sent[0])
        self.assertFalse(channel.sent[0]["allowed_mentions"].everyone)
        self.assertFalse(channel.sent[0]["allowed_mentions"].roles)
        self.assertEqual(channel.sent[0]["embed"].title, RELEASE_BOARD_TITLE)
        self.assertIn("current club book", channel.sent[0]["embed"].description)
        self.assertIn("nominate", channel.sent[0]["embed"].description)
        self.assertEqual(
            database.get_release_panel(self.guild_id),
            {"message_id": "9000", "channel_id": "10"},
        )

    def test_unchanged_board_skips_edit_and_repost(self):
        channel = FakeBoardChannel(10, "book-bot")
        self._sync(channel)
        channel.sent.clear()
        message = channel.messages[9000]

        posted, _guild = self._sync(channel)

        self.assertFalse(posted)
        self.assertEqual(channel.sent, [])
        self.assertEqual(message.edits, [])

    def test_new_release_edits_in_place(self):
        channel = FakeBoardChannel(10, "book-bot")
        self._sync(channel)
        channel.sent.clear()
        newer = (
            ReleaseAnnouncement("2026-09-03", "changelog board", ("One embed, edited.",)),
        ) + RELEASES

        posted, _guild = self._sync(channel, announcements=newer)

        self.assertTrue(posted)
        self.assertEqual(channel.sent, [])
        message = channel.messages[9000]
        self.assertEqual(len(message.edits), 1)
        self.assertIsNone(message.edits[0]["content"])
        self.assertIn("changelog board", message.edits[0]["embed"].description)
        self.assertEqual(
            database.get_release_panel(self.guild_id),
            {"message_id": "9000", "channel_id": "10"},
        )

    def test_failed_post_is_not_recorded(self):
        channel = FakeBoardChannel(10, "book-bot")

        async def boom(**kwargs):
            raise RuntimeError("no access")

        channel.send = boom
        with mock.patch.object(release_announcements, "_log_delivery_failure"):
            posted, _guild = self._sync(channel)

        self.assertFalse(posted)
        self.assertEqual(
            database.get_release_panel(self.guild_id),
            {"message_id": None, "channel_id": None},
        )

    def test_cancelled_post_can_retry(self):
        channel = FakeBoardChannel(10, "book-bot")
        original_send = channel.send

        async def cancel_once(**kwargs):
            channel.send = original_send
            raise asyncio.CancelledError()

        channel.send = cancel_once
        guild = FakeBoardGuild(self.guild_id, [channel])
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(release_announcements.sync_release_board(guild, RELEASES))
        self.assertEqual(
            database.get_release_panel(self.guild_id),
            {"message_id": None, "channel_id": None},
        )

        posted = asyncio.run(release_announcements.sync_release_board(guild, RELEASES))
        self.assertTrue(posted)
        self.assertEqual(
            database.get_release_panel(self.guild_id),
            {"message_id": "9000", "channel_id": "10"},
        )

    def test_overlapping_sync_posts_once(self):
        channel = FakeBoardChannel(10, "book-bot")
        guild = FakeBoardGuild(self.guild_id, [channel])
        send_started = asyncio.Event()
        finish_send = asyncio.Event()
        original_send = channel.send

        async def wait_to_send(**kwargs):
            send_started.set()
            await finish_send.wait()
            return await original_send(**kwargs)

        async def run_overlapping():
            channel.send = wait_to_send
            first = asyncio.create_task(
                release_announcements.sync_release_board(guild, RELEASES)
            )
            await send_started.wait()
            second = asyncio.create_task(
                release_announcements.sync_release_board(guild, RELEASES)
            )
            finish_send.set()
            return await first, await second

        first, second = asyncio.run(run_overlapping())
        self.assertEqual((first, second), (True, False))
        self.assertEqual(len(channel.sent), 1)

    def test_guild_opt_out_skips_post(self):
        import actions_social

        channel = FakeBoardChannel(10, "book-bot")
        actions_social.set_guild_release_announcements_enabled(self.guild_id, False)

        posted, _guild = self._sync(channel)

        self.assertFalse(posted)
        self.assertEqual(channel.sent, [])

    def test_global_switch_off_skips_all_guilds(self):
        channel = FakeBoardChannel(10, "book-bot")
        guild = FakeBoardGuild(self.guild_id, [channel])
        with mock.patch.object(
            release_announcements.config, "RELEASE_ANNOUNCEMENTS_ENABLED", False
        ):
            updated = asyncio.run(
                release_announcements.sync_release_boards([guild], RELEASES)
            )
        self.assertEqual(updated, 0)
        self.assertEqual(channel.sent, [])

    def test_fetch_forbidden_preserves_stored_board_without_reposting(self):
        channel = ForbiddenBoardChannel(10, "book-bot")
        database.set_release_panel_message_id(self.guild_id, 555, 10)

        posted, _guild = self._sync(channel)

        self.assertFalse(posted)
        self.assertEqual(channel.sent, [])
        self.assertEqual(
            database.get_release_panel(self.guild_id),
            {"message_id": "555", "channel_id": "10"},
        )

    def test_channel_change_moves_the_board(self):
        old = FakeBoardChannel(10, "old-bot")
        target = FakeBoardChannel(20, "book-bot")
        panel = old.add_message(555)
        database.set_release_panel_message_id(self.guild_id, 555, 10)

        posted, _guild = self._sync(old, target)

        self.assertTrue(posted)
        self.assertTrue(panel.deleted)
        self.assertEqual(len(target.sent), 1)
        self.assertEqual(
            database.get_release_panel(self.guild_id),
            {"message_id": "9000", "channel_id": "20"},
        )

    def test_stale_id_reuses_existing_board(self):
        channel = FakeBoardChannel(10, "book-bot")
        panel = channel.add_message(555, description="stale")
        database.set_release_panel_message_id(self.guild_id, 999, 10)

        posted, _guild = self._sync(channel)

        self.assertTrue(posted)
        self.assertEqual(channel.sent, [])
        self.assertEqual(len(panel.edits), 1)
        self.assertEqual(
            database.get_release_panel(self.guild_id),
            {"message_id": "555", "channel_id": "10"},
        )

    def test_stale_id_without_history_access_does_not_repost(self):
        channel = HistoryForbiddenChannel(10, "book-bot")
        database.set_release_panel_message_id(self.guild_id, 999, 10)

        posted, _guild = self._sync(channel)

        self.assertFalse(posted)
        self.assertEqual(channel.sent, [])
        self.assertEqual(
            database.get_release_panel(self.guild_id),
            {"message_id": "999", "channel_id": "10"},
        )

    def test_empty_announcements_do_not_post(self):
        channel = FakeBoardChannel(10, "book-bot")

        posted, _guild = self._sync(channel, announcements=())

        self.assertFalse(posted)
        self.assertEqual(channel.sent, [])

    def test_human_whats_new_message_is_not_reused(self):
        channel = FakeBoardChannel(10, "book-bot")
        human = channel.add_message(555)
        human.author = mock.Mock(bot=False)

        posted, _guild = self._sync(channel)

        self.assertTrue(posted)
        self.assertEqual(human.edits, [])
        self.assertEqual(len(channel.sent), 1)


if __name__ == "__main__":
    unittest.main()
