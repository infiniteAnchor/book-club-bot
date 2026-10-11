"""Offline coverage for scrapbook persistence and session scoping."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
from actions import action_finish_book, action_rate_book, action_start_book
from actions_scrapbook import (
    add_scrapbook_entry,
    get_scrapbook,
    list_scrapbook_sessions,
    remove_scrapbook_entry,
)
from actions_session import complete_club_read, start_club_read
from database import connect


class ScrapbookDataTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmpdir.name, "bookclub.db")
        self._db_patch = mock.patch.object(database, "DB_PATH", self.db_path)
        self._db_patch.start()
        database.init_db()
        self.guild = "guild-1"
        self.other_guild = "guild-2"

    def tearDown(self):
        self._db_patch.stop()
        self._tmpdir.cleanup()

    def _start(self, guild=None, title="Dune"):
        _ok, _status, session = start_club_read(
            guild or self.guild, "starter", {"title": title}
        )
        return session

    def test_migration_12_is_idempotent_and_entries_survive_reopen(self):
        session = self._start()
        ok, status, entry = add_scrapbook_entry(
            self.guild, "alice", "Alice", {"content": "A memorable line", "kind": "quote"}
        )
        self.assertTrue(ok)
        self.assertEqual(status, "added")
        self.assertEqual(entry["session_id"], session["session_id"])

        database.init_db()
        conn = connect()
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM schema_version WHERE version = 12").fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM scrapbook_entries").fetchone()[0], 1
            )
        finally:
            conn.close()
        scrapbook = get_scrapbook(self.guild, session_id=session["session_id"])
        self.assertEqual(scrapbook["entries"][0]["content"], "A memorable line")

    def test_session_selection_handles_active_completed_and_repeated_book(self):
        first = self._start(title="Dune")
        add_scrapbook_entry(self.guild, "alice", "Alice", {"content": "first read"})
        self.assertTrue(complete_club_read(self.guild)[0])
        second = self._start(title="Neuromancer")
        self.assertTrue(complete_club_read(self.guild)[0])
        third = self._start(title="Dune")
        self.assertEqual(get_scrapbook(self.guild)["session"]["session_id"], third["session_id"])
        self.assertEqual(
            get_scrapbook(self.guild, title="dUnE")["session"]["session_id"],
            third["session_id"],
        )
        self.assertTrue(complete_club_read(self.guild)[0])
        self.assertEqual(get_scrapbook(self.guild)["session"]["session_id"], third["session_id"])
        self.assertEqual(get_scrapbook(self.guild, session_id=first["session_id"])["entries"][0]["content"], "first read")
        self.assertIsNone(get_scrapbook(self.guild, session_id=99999))
        self.assertIsNone(get_scrapbook(self.other_guild, session_id=first["session_id"]))
        self.assertEqual([s["session_id"] for s in list_scrapbook_sessions(self.guild)], [third["session_id"], second["session_id"], first["session_id"]])

    def test_add_and_delete_are_guild_scoped_and_owner_only(self):
        session = self._start()
        _ok, _status, entry = add_scrapbook_entry(
            self.guild, "alice", "Alice", {"content": "Keep this"}
        )
        self.assertFalse(remove_scrapbook_entry(self.guild, "bob", entry["entry_id"]))
        self.assertFalse(remove_scrapbook_entry(self.other_guild, "alice", entry["entry_id"]))
        self.assertEqual(
            add_scrapbook_entry(
                self.other_guild, "alice", "Alice",
                {"content": "Cross-guild attempt", "session_id": session["session_id"]},
            )[1],
            "invalid_session",
        )
        self.assertTrue(remove_scrapbook_entry(self.guild, "alice", entry["entry_id"]))
        self.assertFalse(remove_scrapbook_entry(self.guild, "alice", entry["entry_id"]))
        self.assertEqual(get_scrapbook(self.guild, session_id=session["session_id"])["entries"], [])

    def test_validation_and_per_session_bounds(self):
        session = self._start()
        cases = [
            (True, {"content": "text"}, "no_session"),
            ("alice", {"content": " "}, "invalid_content"),
            ("alice", {"content": "x" * 1001}, "invalid_content"),
            ("alice", {"content": "x", "kind": "other"}, "invalid_kind"),
            ("alice", {"content": "x", "session_id": True}, "invalid_session"),
            ("alice", {"content": "x", "session_id": 1.5}, "invalid_session"),
            ("alice", {"content": "x", "session_id": 2**100}, "invalid_session"),
            ("alice", {"content": "x", "session_id": str(2**100)}, "invalid_session"),
            ("alice", {"content": "x", "session_id": 99999}, "invalid_session"),
        ]
        for user_id, args, expected in cases:
            with self.subTest(args=args, user_id=user_id):
                self.assertEqual(add_scrapbook_entry(self.guild, user_id, "Alice", args)[1], expected)
        self.assertFalse(remove_scrapbook_entry(self.guild, "alice", True))
        self.assertFalse(remove_scrapbook_entry(self.guild, "alice", 2**100))
        self.assertFalse(remove_scrapbook_entry(self.guild, "alice", str(2**100)))
        self.assertIsNone(get_scrapbook(self.guild, session_id=2**100))
        self.assertIsNone(get_scrapbook(self.guild, session_id=str(2**100)))
        for idx in range(100):
            ok, status, _entry = add_scrapbook_entry(
                self.guild, "alice", "Alice", {"content": f"note {idx}"}
            )
            self.assertTrue(ok, status)
        self.assertEqual(
            add_scrapbook_entry(self.guild, "alice", "Alice", {"content": "one too many"})[1],
            "full",
        )
        self.assertEqual(get_scrapbook(self.guild, session_id=session["session_id"])["entries"].__len__(), 100)

    def test_aggregates_are_guild_lifetime_and_exclude_goodreads_imports(self):
        session = self._start()
        self.assertTrue(action_start_book("alice", "Alice", self.guild, {"title": "Dune"}))
        self.assertTrue(action_finish_book("alice", "Alice", self.guild, {"title": "Dune"}))
        self.assertTrue(action_rate_book("alice", "Alice", {"title": "Dune", "score": 8}, self.guild))
        self.assertTrue(action_start_book("bob", "Bob", self.guild, {"title": "Dune"}))
        self.assertTrue(action_finish_book("bob", "Bob", self.guild, {"title": "Dune"}))
        self.assertTrue(action_rate_book("bob", "Bob", {"title": "Dune", "score": 10}, self.guild))
        conn = connect()
        try:
            conn.execute(
                """UPDATE ratings SET source = 'goodreads'
                   WHERE user_id = (SELECT user_id FROM users WHERE discord_id = 'bob')"""
            )
            conn.execute(
                """UPDATE reading_progress SET source = 'goodreads'
                   WHERE user_id = (SELECT user_id FROM users WHERE discord_id = 'bob')
                     AND guild_id = ?""",
                (self.guild,),
            )
            conn.commit()
        finally:
            conn.close()
        self.assertTrue(action_start_book("cara", "Cara", self.other_guild, {"title": "Dune"}))
        self.assertTrue(action_finish_book("cara", "Cara", self.other_guild, {"title": "Dune"}))
        scrapbook = get_scrapbook(self.guild, session_id=session["session_id"])
        self.assertEqual(scrapbook["finishes"], 1)
        self.assertEqual(scrapbook["ratings"], {"count": 1, "average": 8.0})


if __name__ == "__main__":
    unittest.main()
