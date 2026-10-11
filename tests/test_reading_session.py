"""CI-safe coverage for the canonical shared reading session."""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
from actions import action_start_book, action_update_progress, build_user_context
from actions_session import (
    complete_club_read,
    get_active_session,
    get_current_read,
    maybe_start_from_poll_winner,
    parse_target_date,
    set_club_target,
    start_club_read,
)
from embeds import build_current_read_embed
from views_dashboard import build_admin_dashboard_embed


class ReadingSessionTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmpdir.name, "bookclub.db")
        self._db_patch = mock.patch.object(database, "DB_PATH", self.db_path)
        self._db_patch.start()
        database.init_db()
        self.guild = "guild-1"
        self.other = "guild-2"
        self.alice = ("alice", "Alice")

    def tearDown(self):
        self._db_patch.stop()
        self._tmpdir.cleanup()

    def test_parse_target_date_iso_relative_and_clear(self):
        now = datetime(2026, 9, 2, tzinfo=timezone.utc)
        self.assertEqual(parse_target_date("2026-10-15"), ("2026-10-15", "ok"))
        self.assertEqual(parse_target_date("in 2 weeks", now=now), ("2026-09-16", "ok"))
        self.assertEqual(parse_target_date("tomorrow", now=now), ("2026-09-03", "ok"))
        self.assertEqual(parse_target_date("clear"), (None, "cleared"))
        self.assertEqual(parse_target_date("not a date"), (None, "invalid"))
        self.assertEqual(
            parse_target_date("in 1000000000 weeks", now=now),
            (None, "invalid"),
        )

    def test_start_show_target_complete_and_guild_isolation(self):
        ok, status, session = start_club_read(
            self.guild,
            self.alice[0],
            {"title": "Dune", "author": "Frank Herbert", "target_date": "2026-10-15"},
        )
        self.assertTrue(ok)
        self.assertEqual(status, "started")
        self.assertEqual(session["title"], "Dune")
        self.assertEqual(session["target_date"], "2026-10-15")

        current = get_current_read(self.guild)
        self.assertIsNotNone(current)
        self.assertEqual(current["title"], "Dune")
        self.assertEqual(current["members"], [])
        self.assertIsNone(get_active_session(self.other))

        ok, status, session = set_club_target(self.guild, self.alice[0], "clear")
        self.assertTrue(ok)
        self.assertEqual(status, "cleared")
        self.assertIsNone(session["target_date"])

        ok, status, _session = complete_club_read(self.guild, self.alice[0])
        self.assertTrue(ok)
        self.assertEqual(status, "completed")
        self.assertIsNone(get_active_session(self.guild))

    def test_one_active_session_per_guild_unless_replaced(self):
        start_club_read(self.guild, self.alice[0], {"title": "Dune"})
        ok, status, existing = start_club_read(
            self.guild, self.alice[0], {"title": "Neuromancer"}
        )
        self.assertFalse(ok)
        self.assertEqual(status, "already_active")
        self.assertEqual(existing["title"], "Dune")

        ok, status, session = start_club_read(
            self.guild, self.alice[0], {"title": "Neuromancer"}, replace=True
        )
        self.assertTrue(ok)
        self.assertEqual(status, "replaced")
        self.assertEqual(session["title"], "Neuromancer")
        self.assertEqual(get_active_session(self.guild)["title"], "Neuromancer")

        ok, status, session = start_club_read(
            self.guild, self.alice[0], {"title": "Neuromancer"}
        )
        self.assertTrue(ok)
        self.assertEqual(status, "already")
        self.assertEqual(session["title"], "Neuromancer")

    def test_rejected_start_does_not_create_book(self):
        start_club_read(self.guild, self.alice[0], {"title": "Dune"})

        ok, status, _session = start_club_read(
            self.guild,
            self.alice[0],
            {"title": "Hamlet", "author": "Wrong Author"},
        )

        self.assertFalse(ok)
        self.assertEqual(status, "already_active")
        conn = database.connect()
        try:
            row = conn.execute(
                "SELECT book_id FROM books WHERE LOWER(title) = LOWER(?)", ("Hamlet",)
            ).fetchone()
            self.assertIsNone(row)
        finally:
            conn.close()

    def test_member_progress_and_user_context(self):
        start_club_read(self.guild, self.alice[0], {"title": "Dune"})
        action_start_book(self.alice[0], self.alice[1], self.guild, {"title": "Dune"})
        action_update_progress(
            self.alice[0],
            self.alice[1],
            self.guild,
            {"title": "Dune", "progress_raw": 40, "progress_unit": "percent"},
        )
        current = get_current_read(self.guild)
        self.assertEqual(len(current["members"]), 1)
        self.assertEqual(current["members"][0]["display_name"], "Alice")
        self.assertEqual(current["members"][0]["status"], "reading")

        context = build_user_context(self.alice[0], self.guild)
        self.assertEqual(context["club_read"]["title"], "Dune")

        embed = build_current_read_embed(current)
        self.assertIn("Dune", embed.title)
        self.assertTrue(any(field.name == "Members" for field in embed.fields))

        empty = build_current_read_embed(None)
        self.assertIn("does not have a current book", empty.description)

    def test_poll_winner_starts_only_when_idle(self):
        winner = {"title": "Foundation", "author": "Isaac Asimov", "book_id": None}
        session = maybe_start_from_poll_winner(self.guild, self.alice[0], winner, poll_id=9)
        self.assertIsNotNone(session)
        self.assertEqual(session["title"], "Foundation")
        self.assertEqual(session["poll_id"], 9)

        skipped = maybe_start_from_poll_winner(
            self.guild,
            self.alice[0],
            {"title": "Dune", "author": "Frank Herbert"},
            poll_id=10,
        )
        self.assertIsNone(skipped)
        self.assertEqual(get_active_session(self.guild)["title"], "Foundation")

    def test_dashboard_overview_names_the_club_book(self):
        start_club_read(
            self.guild, self.alice[0], {"title": "Dune", "target_date": "2026-10-15"}
        )
        embed = build_admin_dashboard_embed(self.guild)
        self.assertIn("Club book: **Dune**", embed.description)
        self.assertIn("target 2026-10-15", embed.description)

    def test_unique_active_index_exists(self):
        conn = sqlite3.connect(self.db_path)
        try:
            indexes = {
                row[1]
                for row in conn.execute("PRAGMA index_list(reading_sessions)").fetchall()
            }
            self.assertIn("reading_sessions_one_active_per_guild", indexes)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
