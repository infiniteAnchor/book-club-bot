"""Club history archive: finished-books query, registry, and /history wiring."""
import asyncio
import os
import sys
import tempfile
import unittest
from unittest import mock
import database
import handlers
from actions import action_finish_book, action_rate_book, action_start_book
from actions_engagement import get_club_history
from actions_social import get_club_history as facade_history
from commands import slash_history
from database import connect
from handlers import INTENT_ACTIONS
from llm import VALID_ACTIONS


class ClubHistoryTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmpdir.name, "bookclub.db")
        self._db_patch = mock.patch.object(database, "DB_PATH", self.db_path)
        self._db_patch.start()
        database.init_db()
        self.guild = "guild-1"
        self.other = "guild-2"

    def tearDown(self):
        self._db_patch.stop()
        self._tmpdir.cleanup()

    def _finish(self, uid, name, guild, title, finished_at):
        action_start_book(uid, name, guild, {"title": title})
        self.assertTrue(action_finish_book(uid, name, guild, {"title": title}))
        conn = connect()
        try:
            conn.execute(
                """UPDATE reading_progress SET finished_at = ?
                   WHERE user_id = (SELECT user_id FROM users WHERE discord_id = ?)
                   AND book_id = (SELECT book_id FROM books WHERE title = ?)
                   AND guild_id = ?""",
                (finished_at, uid, title, guild),
            )
            conn.commit()
        finally:
            conn.close()

    def test_empty_guild_has_no_history(self):
        self.assertEqual(get_club_history(self.guild), [])

    def test_recent_first_with_finishers_ratings_and_guild_isolation(self):
        self._finish("alice", "Alice", self.guild, "Dune", "2026-06-01 10:00:00")
        self._finish("bob", "Bob", self.guild, "Dune", "2026-06-02 10:00:00")
        self._finish("alice", "Alice", self.guild, "Klara", "2026-08-01 10:00:00")
        action_start_book("alice", "Alice", self.guild, {"title": "Abandoned"})
        action_finish_book("alice", "Alice", self.guild, {"title": "Abandoned"})
        conn = connect()
        try:
            conn.execute(
                """UPDATE reading_progress SET status = 'dnf'
                   WHERE book_id = (SELECT book_id FROM books WHERE title = 'Abandoned')"""
            )
            conn.commit()
        finally:
            conn.close()
        self._finish("alice", "Alice", self.other, "Other Guild Book", "2026-09-01 10:00:00")
        self.assertTrue(
            action_rate_book("alice", "Alice", {"title": "Dune", "score": 9}, self.guild)
        )
        self.assertTrue(
            action_rate_book("bob", "Bob", {"title": "Dune", "score": 7}, self.guild)
        )

        history = get_club_history(self.guild)
        self.assertEqual([row["title"] for row in history], ["Klara", "Dune"])
        dune = next(row for row in history if row["title"] == "Dune")
        self.assertEqual(dune["finishes"], 2)
        self.assertIn("Alice", dune["finishers"])
        self.assertIn("Bob", dune["finishers"])
        self.assertEqual(dune["avg_score"], 8.0)
        self.assertEqual(dune["ratings_count"], 2)
        klara = next(row for row in history if row["title"] == "Klara")
        self.assertNotIn("avg_score", klara)
        self.assertIsNotNone(klara["last_finished_at"])

    def test_limit_is_capped(self):
        for i in range(55):
            self._finish("alice", "Alice", self.guild, f"Book {i:02d}", "2026-01-01 10:00:00")
        self.assertEqual(len(get_club_history(self.guild)), 50)
        self.assertEqual(len(get_club_history(self.guild, limit=5)), 5)

    def test_registry_and_slash_wiring(self):
        self.assertIn("club_history", VALID_ACTIONS)
        self.assertIn("club_history", INTENT_ACTIONS)
        self.assertIs(facade_history, get_club_history)
        from commands import COMMAND_DESCRIPTIONS, register

        class FakeTree:
            def __init__(self):
                self.cmds = {}

            def command(self, name, description):
                def decorator(fn):
                    self.cmds[name] = (fn, description)
                    return fn

                return decorator

        tree = FakeTree()
        register(tree)
        self.assertIs(tree.cmds["history"][0], slash_history)
        self.assertEqual(tree.cmds["history"][1], COMMAND_DESCRIPTIONS["history"])

class LeaderboardHandlerRegressionTests(unittest.TestCase):
    """The history branch once dropped this import; execute the handler."""

    def _ctx(self):
        message = mock.Mock()
        message.author.id = 1001
        return handlers.RequestContext(
            message=message,
            guild_id="guild-1",
            author_id="1001",
            display_name="Alice",
            reply_channel=mock.Mock(),
        )

    def test_leaderboard_handler_empty_stats(self):
        sent = []

        async def fake_send(channel, pages, **kwargs):
            sent.append(pages)

        with mock.patch.object(handlers, "get_leaderboard_stats", return_value=[]), mock.patch.object(
            handlers, "send_embeds", fake_send
        ):
            asyncio.run(handlers._handle_leaderboard(self._ctx(), {}, {}, None))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0].title, "🏆 Club leaderboard")

    def test_leaderboard_handler_with_stats(self):
        sent = []

        async def fake_send(channel, pages, **kwargs):
            sent.append(pages)

        stats = [{"name": "Alice", "books_finished": 2, "pages_read": 600, "dnf_count": 0}]
        with mock.patch.object(handlers, "get_leaderboard_stats", return_value=stats), mock.patch.object(
            handlers.llm, "get_leaderboard_narrative", return_value="Go club!"
        ), mock.patch.object(handlers, "send_embeds", fake_send):
            asyncio.run(handlers._handle_leaderboard(self._ctx(), {}, {}, None))
        self.assertEqual(len(sent), 1)
        self.assertIn("Go club!", sent[0][0].description)

class ClubHistoryHandlerTests(unittest.TestCase):
    """Chat history stays Claude-authored; the narrative leads the embed."""

    def test_handler_sets_claude_narrative_as_description(self):
        sent = []

        async def fake_send(channel, pages, **kwargs):
            sent.append(pages)

        history = [
            {
                "book_id": 1,
                "title": "Dune",
                "author": "Frank Herbert",
                "finishes": 2,
                "finishers": "Alice, Bob",
                "avg_score": 8.0,
                "ratings_count": 2,
                "last_finished_at": "2026-06-02 10:00:00",
            }
        ]
        message = mock.Mock()
        message.author.id = 1001
        ctx = handlers.RequestContext(
            message=message,
            guild_id="guild-1",
            author_id="1001",
            display_name="Alice",
            reply_channel=mock.Mock(),
        )
        with mock.patch.object(handlers, "get_club_history", return_value=history), mock.patch.object(
            handlers.llm, "get_club_history_narrative", return_value="What a shelf!"
        ), mock.patch.object(handlers, "send_embeds", fake_send):
            asyncio.run(handlers._handle_club_history(ctx, {}, {}, None))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0].title, "📚 Club history")
        self.assertIn("What a shelf!", sent[0][0].description)


if __name__ == "__main__":
    unittest.main()
