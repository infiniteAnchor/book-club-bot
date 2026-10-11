import asyncio
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

import discord

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
import guild_settings


class GuildSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "bookclub.db")
        self.db_patch = mock.patch.object(database, "DB_PATH", self.db_path)
        self.db_patch.start()
        database.init_db()

    def tearDown(self):
        self.db_patch.stop()
        self.tmpdir.cleanup()

    def test_ensure_inserts_current_config_defaults(self):
        settings = guild_settings.ensure_guild_settings("guild-1")
        defaults = guild_settings.default_settings()
        for key in guild_settings.SETTING_KEYS:
            self.assertEqual(settings[key], defaults[key], key)

        conn = sqlite3.connect(self.db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM guild_settings").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 1)

    def test_ensure_is_idempotent_and_ignores_later_config_defaults(self):
        original = guild_settings.ensure_guild_settings("guild-1")
        self.assertTrue(original["weekly_recap_scheduled"])

        guild_settings.update_guild_settings("guild-1", weekly_recap_scheduled=False, nominations_channel="picks")
        with mock.patch.object(guild_settings.config, "WEEKLY_RECAP_SCHEDULED", True), \
             mock.patch.object(guild_settings.config, "NOMINATIONS_CHANNEL", "new-default"):
            again = guild_settings.ensure_guild_settings("guild-1")

        self.assertFalse(again["weekly_recap_scheduled"])
        self.assertEqual(again["nominations_channel"], "picks")

    def test_new_guild_picks_up_current_config_defaults(self):
        first = guild_settings.ensure_guild_settings("guild-1")
        with mock.patch.object(guild_settings.config, "NOMINATIONS_CHANNEL", "fresh-channel"), \
             mock.patch.object(guild_settings.config, "REQUIRE_MENTION", False):
            second = guild_settings.ensure_guild_settings("guild-2")
            reread_first = guild_settings.ensure_guild_settings("guild-1")
        self.assertEqual(second["nominations_channel"], "fresh-channel")
        self.assertFalse(second["require_mention"])
        self.assertEqual(reread_first["nominations_channel"], first["nominations_channel"])
        self.assertEqual(first["nominations_channel"], "book-bot")

    def test_get_creates_missing_row(self):
        settings = guild_settings.get_guild_settings("late-guild")
        self.assertEqual(settings["nominations_channel"], guild_settings.default_settings()["nominations_channel"])

    def test_update_rejects_unknown_keys_and_bad_values(self):
        guild_settings.ensure_guild_settings("guild-1")
        with self.assertRaises(guild_settings.SettingsError):
            guild_settings.update_guild_settings("guild-1", not_a_real_key="x")
        with self.assertRaises(guild_settings.SettingsError):
            guild_settings.update_guild_settings("guild-1", weekly_recap_hour=24)
        with self.assertRaises(guild_settings.SettingsError):
            guild_settings.update_guild_settings("guild-1", weekly_recap_day_of_week="funday")
        with self.assertRaises(guild_settings.SettingsError):
            guild_settings.update_guild_settings("guild-1", nominations_channel="")
        with self.assertRaises(guild_settings.SettingsError):
            guild_settings.update_guild_settings("guild-1", nominations_default_days=0)
        unchanged = guild_settings.get_guild_settings("guild-1")
        self.assertEqual(unchanged["weekly_recap_hour"], guild_settings.default_settings()["weekly_recap_hour"])

    def test_update_strips_hash_and_toggles_bools(self):
        updated = guild_settings.update_guild_settings(
            "guild-1",
            nominations_channel="#votes",
            require_mention=False,
            reply_channel_name="",
        )
        self.assertEqual(updated["nominations_channel"], "votes")
        self.assertFalse(updated["require_mention"])
        self.assertEqual(updated["reply_channel_name"], "")

    def test_listen_only_requires_a_listen_channel(self):
        with self.assertRaisesRegex(guild_settings.SettingsError, "Set a listen channel"):
            guild_settings.update_guild_settings("guild-1", listen_channel_only=True)
        updated = guild_settings.update_guild_settings(
            "guild-1", listen_channel_name="#reading", listen_channel_only=True
        )
        self.assertTrue(updated["listen_channel_only"])
        with self.assertRaisesRegex(guild_settings.SettingsError, "Set a listen channel"):
            guild_settings.update_guild_settings("guild-1", listen_channel_name="")

    def test_missing_guild_id_is_rejected(self):
        with self.assertRaises(ValueError):
            guild_settings.ensure_guild_settings("")

    def test_settings_dashboard_embed_reads_the_guild_row(self):
        from views import build_admin_dashboard_embed

        guild_settings.update_guild_settings("guild-1", nominations_channel="picks")
        embed = build_admin_dashboard_embed("guild-1", "settings")
        fields = {field.name: field.value for field in embed.fields}
        self.assertIn("#picks", fields["Channels"])
        self.assertIn("config.py", cast_str := str(embed.description))
        self.assertIn("do not rewrite", cast_str)

    def test_bot_wires_ensure_on_guild_init(self):
        src = open("bot.py").read()
        self.assertIn("ensure_guild_settings(guild_id)", src)
        self.assertIn("schedule_guild_jobs(guild_id)", src)
        self.assertIn("async def on_guild_remove", src)
        self.assertIn("cancel_guild_jobs", src)


class GuildJobScheduleTests(unittest.TestCase):
    def test_schedule_guild_jobs_uses_row_not_config_and_can_reschedule(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "bookclub.db")
            with mock.patch.object(database, "DB_PATH", db_path):
                database.init_db()
                guild_settings.update_guild_settings(
                    "guild-1",
                    combined_weekly_post=True,
                    weekly_recap_day_of_week="sun",
                    weekly_recap_hour=11,
                    inactivity_nudge_scheduled=False,
                )
                added = []

                class FakeScheduler:
                    def get_job(self, job_id):
                        return next((item for item in added if item["id"] == job_id), None)

                    def remove_job(self, job_id):
                        added[:] = [item for item in added if item["id"] != job_id]

                    def add_job(self, func, trigger, **kwargs):
                        added.append({"func": func, "trigger": trigger, **kwargs})

                import jobs
                with mock.patch.object(jobs, "scheduler", FakeScheduler()):
                    jobs.schedule_guild_jobs("guild-1")
                    self.assertEqual(
                        [item["id"] for item in added],
                        ["combined_weekly_post:guild-1"],
                    )
                    self.assertEqual(added[0]["day_of_week"], "sun")
                    self.assertEqual(added[0]["hour"], 11)

                    guild_settings.update_guild_settings(
                        "guild-1",
                        combined_weekly_post=False,
                        weekly_recap_scheduled=True,
                        checkin_prompt_scheduled=False,
                        weekly_recap_hour=8,
                    )
                    jobs.schedule_guild_jobs("guild-1")
                    self.assertEqual(
                        [item["id"] for item in added],
                        ["weekly_recap:guild-1"],
                    )
                    self.assertEqual(added[0]["hour"], 8)

    def test_cancel_guild_jobs_removes_periodic_and_auto_close_jobs(self):
        import jobs

        removed = []

        class FakeScheduler:
            def get_job(self, job_id):
                return object()

            def remove_job(self, job_id):
                removed.append(job_id)

        registries = (
            (jobs.nomination_jobs, "nomination-close"),
            (jobs.poll_jobs, "poll-close"),
            (jobs.coordination_poll_jobs, "coordination-close"),
        )
        for registry, job_id in registries:
            registry["guild-1"] = job_id

        try:
            with mock.patch.object(jobs, "scheduler", FakeScheduler()):
                jobs.cancel_guild_jobs("guild-1")
        finally:
            for registry, _ in registries:
                registry.pop("guild-1", None)

        self.assertEqual(
            removed,
            [
                "combined_weekly_post:guild-1",
                "weekly_recap:guild-1",
                "periodic_checkin_prompt:guild-1",
                "inactivity_nudge_sweep:guild-1",
                "nomination-close",
                "auto-close:nominations:guild-1",
                "poll-close",
                "auto-close:poll:guild-1",
                "coordination-close",
                "auto-close:coordination:guild-1",
            ],
        )


class GuildSettingsMigrationTests(unittest.TestCase):
    def test_init_db_applies_guild_settings_migration(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "bookclub.db")
            with mock.patch.object(database, "DB_PATH", db_path):
                database.init_db()
                conn = sqlite3.connect(db_path)
                try:
                    versions = conn.execute(
                        "SELECT version FROM schema_version ORDER BY version"
                    ).fetchall()
                    self.assertEqual(
                        versions,
                        [(version,) for version, _migration in database.MIGRATIONS],
                    )
                    columns = {
                        row[1]
                        for row in conn.execute("PRAGMA table_info(guild_settings)")
                    }
                    self.assertTrue({"guild_id", *guild_settings.SETTING_KEYS}.issubset(columns))
                    server_columns = {
                        row[1]
                        for row in conn.execute("PRAGMA table_info(servers)")
                    }
                    self.assertIn("enrollment_channel_id", server_columns)
                    self.assertIn("release_announcement_message_id", server_columns)
                    self.assertIn("release_announcement_channel_id", server_columns)
                finally:
                    conn.close()


class FakeEnrollmentMessage:
    def __init__(self, message_id, channel):
        self.id = message_id
        self.channel = channel
        self.edits = []
        self.deleted = False
        self.mentions = []
        self.embeds = []
        self.components = []
        self.author = None

    async def edit(self, **kwargs):
        self.edits.append(kwargs)

    async def delete(self):
        self.deleted = True
        self.channel.messages.pop(int(self.id), None)

    def as_panel(self):
        self.embeds = [mock.Mock(title="👋 Join Book Club")]
        return self


class FakeEnrollmentChannel:
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
        message = FakeEnrollmentMessage(self.next_message_id, self)
        self.next_message_id += 1
        self.messages[message.id] = message
        self.sent.append(kwargs)
        return message

    def add_message(self, message_id):
        message = FakeEnrollmentMessage(message_id, self)
        self.messages[int(message_id)] = message
        return message


class ForbiddenEnrollmentChannel(FakeEnrollmentChannel):
    async def fetch_message(self, message_id):
        raise discord.Forbidden(mock.MagicMock(status=403), {"message": "Missing Access"})


class HistoryForbiddenChannel(FakeEnrollmentChannel):
    def history(self, *, limit=100, **kwargs):
        raise discord.Forbidden(mock.MagicMock(status=403), {"message": "Missing Access"})


class FakeEnrollmentGuild:
    def __init__(self, guild_id, channels):
        self.id = int(guild_id)
        self.text_channels = list(channels)
        self._channels = {channel.id: channel for channel in channels}

    def get_channel(self, channel_id):
        return self._channels.get(int(channel_id))


class EnrollmentPanelSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "bookclub.db")
        self.db_patch = mock.patch.object(database, "DB_PATH", self.db_path)
        self.db_patch.start()
        database.init_db()
        self.guild_id = "42"
        database.upsert_server(self.guild_id, "Club")
        guild_settings.update_guild_settings(
            self.guild_id,
            member_opt_in_enabled=True,
            opt_in_welcome_channel="welcome",
        )

    def tearDown(self):
        self.db_patch.stop()
        self.tmpdir.cleanup()

    def _sync(self, *channels):
        import jobs

        guild = FakeEnrollmentGuild(self.guild_id, channels)
        with mock.patch.object(jobs, "_resolve_guild", return_value=guild):
            asyncio.run(jobs.sync_enrollment_panel(self.guild_id))
        return guild

    def test_missing_channel_id_edits_existing_welcome_panel(self):
        welcome = FakeEnrollmentChannel(10, "welcome")
        panel = welcome.add_message(555)
        database.set_enrollment_message_id(self.guild_id, 555)

        self._sync(welcome)

        self.assertEqual(len(panel.edits), 1)
        self.assertIsNone(panel.edits[0]["content"])
        self.assertEqual(welcome.sent, [])
        self.assertEqual(
            database.get_enrollment_panel(self.guild_id),
            {"message_id": "555", "channel_id": "10"},
        )

    def test_fetch_forbidden_preserves_stored_panel_without_reposting(self):
        welcome = ForbiddenEnrollmentChannel(10, "welcome")
        database.set_enrollment_message_id(self.guild_id, 555, 10)

        self._sync(welcome)

        self.assertEqual(welcome.sent, [])
        self.assertEqual(
            database.get_enrollment_panel(self.guild_id),
            {"message_id": "555", "channel_id": "10"},
        )

    def test_same_channel_edit_backfills_channel_id(self):
        welcome = FakeEnrollmentChannel(10, "welcome")
        panel = welcome.add_message(555)
        database.set_enrollment_message_id(self.guild_id, 555, 10)

        self._sync(welcome)

        self.assertEqual(len(panel.edits), 1)
        self.assertIsNone(panel.edits[0]["content"])
        self.assertEqual(welcome.sent, [])
        self.assertEqual(
            database.get_enrollment_panel(self.guild_id),
            {"message_id": "555", "channel_id": "10"},
        )

    def test_channel_change_moves_the_panel(self):
        old = FakeEnrollmentChannel(10, "old-welcome")
        welcome = FakeEnrollmentChannel(20, "welcome")
        panel = old.add_message(555)
        database.set_enrollment_message_id(self.guild_id, 555, 10)

        self._sync(old, welcome)

        self.assertTrue(panel.deleted)
        self.assertEqual(len(welcome.sent), 1)
        self.assertNotIn("content", welcome.sent[0])
        self.assertEqual(
            database.get_enrollment_panel(self.guild_id),
            {"message_id": "9000", "channel_id": "20"},
        )

    def test_disable_deletes_stored_panel(self):
        welcome = FakeEnrollmentChannel(10, "welcome")
        panel = welcome.add_message(555)
        database.set_enrollment_message_id(self.guild_id, 555, 10)
        guild_settings.update_guild_settings(self.guild_id, member_opt_in_enabled=False)

        self._sync(welcome)

        self.assertTrue(panel.deleted)
        self.assertEqual(welcome.sent, [])
        self.assertEqual(
            database.get_enrollment_panel(self.guild_id),
            {"message_id": None, "channel_id": None},
        )

    def test_disable_without_channel_id_still_retires_welcome_panel(self):
        welcome = FakeEnrollmentChannel(10, "welcome")
        panel = welcome.add_message(555)
        database.set_enrollment_message_id(self.guild_id, 555)
        guild_settings.update_guild_settings(self.guild_id, member_opt_in_enabled=False)

        self._sync(welcome)

        self.assertTrue(panel.deleted)
        self.assertEqual(
            database.get_enrollment_panel(self.guild_id),
            {"message_id": None, "channel_id": None},
        )

    def test_missing_stored_id_reuses_existing_welcome_panel(self):
        welcome = FakeEnrollmentChannel(10, "welcome")
        panel = welcome.add_message(555).as_panel()

        self._sync(welcome)

        self.assertEqual(len(panel.edits), 1)
        self.assertEqual(welcome.sent, [])
        self.assertEqual(
            database.get_enrollment_panel(self.guild_id),
            {"message_id": "555", "channel_id": "10"},
        )

    def test_stale_stored_id_reuses_existing_welcome_panel(self):
        welcome = FakeEnrollmentChannel(10, "welcome")
        panel = welcome.add_message(555).as_panel()
        database.set_enrollment_message_id(self.guild_id, 999, 10)

        self._sync(welcome)

        self.assertEqual(len(panel.edits), 1)
        self.assertEqual(welcome.sent, [])
        self.assertEqual(
            database.get_enrollment_panel(self.guild_id),
            {"message_id": "555", "channel_id": "10"},
        )

    def test_reuses_panel_identified_by_enroll_button(self):
        welcome = FakeEnrollmentChannel(10, "welcome")
        panel = welcome.add_message(555)
        panel.components = [mock.Mock(children=[mock.Mock(custom_id="enroll_tracking")])]

        self._sync(welcome)

        self.assertEqual(len(panel.edits), 1)
        self.assertEqual(welcome.sent, [])

    def test_join_mention_message_is_not_reused_as_panel(self):
        welcome = FakeEnrollmentChannel(10, "welcome")
        join_msg = welcome.add_message(555).as_panel()
        join_msg.mentions = [object()]

        self._sync(welcome)

        self.assertEqual(join_msg.edits, [])
        self.assertEqual(len(welcome.sent), 1)
        self.assertEqual(
            database.get_enrollment_panel(self.guild_id),
            {"message_id": "9000", "channel_id": "10"},
        )

    def test_missing_panel_posts_one(self):
        welcome = FakeEnrollmentChannel(10, "welcome")

        self._sync(welcome)

        self.assertEqual(len(welcome.sent), 1)
        self.assertEqual(
            database.get_enrollment_panel(self.guild_id),
            {"message_id": "9000", "channel_id": "10"},
        )

    def test_stale_id_without_history_access_does_not_repost(self):
        welcome = HistoryForbiddenChannel(10, "welcome")
        database.set_enrollment_message_id(self.guild_id, 999, 10)

        self._sync(welcome)

        self.assertEqual(welcome.sent, [])
        self.assertEqual(
            database.get_enrollment_panel(self.guild_id),
            {"message_id": "999", "channel_id": "10"},
        )


if __name__ == "__main__":
    unittest.main()
