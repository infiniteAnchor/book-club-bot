"""Channel setting ID/name dual-read helpers and upgrades."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import database
import guild_settings
import helpers


class FakeChannel:
    def __init__(self, channel_id: int, name: str):
        self.id = channel_id
        self.name = name
        self.mention = f"<#{channel_id}>"


class FakeGuild:
    def __init__(self, channels):
        self.id = 42
        self.text_channels = list(channels)


class ChannelSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        self.db_patch = mock.patch.object(database, "DB_PATH", str(self.db_path))
        self.db_patch.start()
        database.init_db()
        guild_settings.ensure_guild_settings("42")

    def tearDown(self):
        self.db_patch.stop()
        self.tmpdir.cleanup()

    def test_coerce_accepts_snowflake_and_name(self):
        updated = guild_settings.update_guild_settings(
            "42",
            nominations_channel="123456789012345678",
            weekly_recap_channel="#recaps",
        )
        self.assertEqual(updated["nominations_channel"], "123456789012345678")
        self.assertEqual(updated["weekly_recap_channel"], "recaps")

    def test_resolve_text_channel_prefers_id_then_name(self):
        book_bot = FakeChannel(111, "book-bot")
        picks = FakeChannel(222, "picks")
        guild = FakeGuild([book_bot, picks])
        self.assertIs(helpers.resolve_text_channel(guild, "111"), book_bot)
        self.assertIs(helpers.resolve_text_channel(guild, "#picks"), picks)
        self.assertIsNone(helpers.resolve_text_channel(guild, "missing"))
        self.assertIsNone(helpers.resolve_text_channel(guild, ""))

    def test_channel_setting_label_and_edit_default(self):
        channel = FakeChannel(111, "book-bot")
        guild = FakeGuild([channel])
        self.assertEqual(helpers.channel_setting_label("111", guild), "<#111>")
        self.assertEqual(helpers.channel_setting_label("book-bot"), "#book-bot")
        self.assertEqual(helpers.channel_setting_label(""), "(not set)")
        self.assertEqual(helpers.channel_setting_edit_default(guild, "111"), "book-bot")
        self.assertEqual(helpers.channel_setting_edit_default(guild, "picks"), "picks")

    def test_channel_matches_setting(self):
        channel = FakeChannel(111, "book-bot")
        self.assertTrue(helpers.channel_matches_setting(channel, "111"))
        self.assertTrue(helpers.channel_matches_setting(channel, "#book-bot"))
        self.assertFalse(helpers.channel_matches_setting(channel, "222"))
        self.assertFalse(helpers.channel_matches_setting(channel, "other"))

    def test_upgrade_rewrites_names_to_ids(self):
        guild_settings.update_guild_settings(
            "42",
            nominations_channel="picks",
            reply_channel_name="",
            listen_channel_name="listening",
        )
        picks = FakeChannel(222, "picks")
        guild = FakeGuild([picks])
        updated = helpers.upgrade_guild_channel_settings(guild)
        self.assertEqual(updated["nominations_channel"], "222")
        # Unresolved optional name is left alone for dual-read.
        self.assertEqual(updated["listen_channel_name"], "listening")
        self.assertEqual(updated["reply_channel_name"], "")

    def test_settings_embed_shows_mention_for_id(self):
        from views import build_admin_dashboard_embed

        guild_settings.update_guild_settings("42", nominations_channel="999888777666555444")
        embed = build_admin_dashboard_embed("42", "settings")
        fields = {field.name: field.value for field in embed.fields}
        self.assertIn("<#999888777666555444>", fields["Channels"])

    def test_bot_wires_channel_upgrade_on_startup(self):
        src = open("bot.py").read()
        self.assertIn("upgrade_guild_channel_settings(guild)", src)


if __name__ == "__main__":
    unittest.main()
