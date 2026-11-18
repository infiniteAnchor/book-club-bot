"""Guild-scoped shelf writes (BCB-007) and add_book wishlist cap (BCB-008)."""
import asyncio
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
from actions import (
    action_add_book,
    action_add_to_wishlist,
    action_dnf_book,
    action_finish_book,
    action_start_book,
    action_update_progress,
    get_user_book_progress,
    query_shelf,
)
from actions_engagement import action_set_goal, get_goal_progress, get_leaderboard_stats
from actions_queue import LIST_CAP, action_wishlist_add
from database import connect


class ShelfGuildScopeTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmpdir.name, "bookclub.db")
        self._db_patch = mock.patch.object(database, "DB_PATH", self.db_path)
        self._db_patch.start()
        database.init_db()
        self.guild_a = "guild-a"
        self.guild_b = "guild-b"
        self.alice = ("alice", "Alice")

    def tearDown(self):
        self._db_patch.stop()
        self._tmpdir.cleanup()

    def _rows(self, title):
        conn = connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """SELECT rp.guild_id, rp.status, rp.progress_raw
                   FROM reading_progress rp
                   JOIN books b ON b.book_id = rp.book_id
                   WHERE b.title = ?
                   ORDER BY rp.progress_id""",
                (title,),
            )
            return cur.fetchall()
        finally:
            conn.close()

    def test_start_progress_finish_stay_in_calling_guild(self):
        uid, name = self.alice
        action_start_book(uid, name, self.guild_a, {"title": "Dune", "author": "Frank Herbert"})
        action_update_progress(
            uid, name, self.guild_a, {"title": "Dune", "progress_raw": 50, "progress_unit": "pages"}
        )

        action_start_book(uid, name, self.guild_b, {"title": "Dune", "author": "Frank Herbert"})
        action_update_progress(
            uid, name, self.guild_b, {"title": "Dune", "progress_raw": 10, "progress_unit": "pages"}
        )
        action_finish_book(uid, name, self.guild_b, {"title": "Dune"})

        rows = self._rows("Dune")
        self.assertEqual(
            [(guild, status, raw) for guild, status, raw in rows],
            [
                (self.guild_a, "reading", 50),
                (self.guild_b, "finished", 10),
            ],
        )
        in_a = get_user_book_progress(uid, "Dune", self.guild_a)
        in_b = get_user_book_progress(uid, "Dune", self.guild_b)
        assert in_a is not None and in_b is not None
        self.assertEqual(in_a["status"], "reading")
        self.assertEqual(in_b["status"], "finished")

    def test_finish_is_repeat_safe_and_new_attempt_counts_as_reread(self):
        uid, name = self.alice
        args = {"title": "Dune"}
        action_set_goal(uid, name, {"goal": 10})
        self.assertIs(action_finish_book(uid, name, self.guild_a, args), True)
        self.assertIsNone(action_finish_book(uid, name, self.guild_a, args))
        self.assertEqual(self._rows("Dune"), [(self.guild_a, "finished", None)])
        self.assertEqual(get_goal_progress(uid, self.guild_a)["progress"], 1)
        self.assertEqual(get_leaderboard_stats(self.guild_a)[0]["books_finished"], 1)

        self.assertTrue(action_dnf_book(uid, name, self.guild_a, args)[0])
        self.assertIsNone(action_finish_book(uid, name, self.guild_a, args))
        self.assertEqual(self._rows("Dune"), [
            (self.guild_a, "finished", None), (self.guild_a, "dnf", None),
        ])
        self.assertEqual(get_goal_progress(uid, self.guild_a)["progress"], 1)
        self.assertEqual(get_leaderboard_stats(self.guild_a)[0]["books_finished"], 1)

        self.assertTrue(action_start_book(uid, name, self.guild_a, args))
        attempt = get_user_book_progress(uid, "Dune", self.guild_a)
        self.assertEqual(attempt["status"], "reading")
        self.assertIs(action_finish_book(uid, name, self.guild_a, args), True)
        self.assertTrue(action_start_book(uid, name, self.guild_a, args))
        self.assertFalse(action_finish_book(
            uid, name, self.guild_a, args, expected_progress_id=attempt["progress_id"],
        ))
        self.assertFalse(action_dnf_book(
            uid, name, self.guild_a, args, expected_progress_id=attempt["progress_id"],
        )[0])
        self.assertEqual(get_goal_progress(uid, self.guild_a)["progress"], 2)
        self.assertEqual(get_leaderboard_stats(self.guild_a)[0]["books_finished"], 2)
        self.assertEqual(get_user_book_progress(uid, "Dune", self.guild_a)["status"], "reading")

    def test_repeated_finish_in_chat_reports_existing_completion(self):
        import handlers

        uid, name = self.alice
        args = {"title": "Dune"}
        self.assertIs(action_finish_book(uid, name, self.guild_a, args), True)
        ctx = handlers.RequestContext(
            message=mock.Mock(), guild_id=self.guild_a, author_id=uid,
            display_name=name, reply_channel=mock.Mock(),
        )
        with mock.patch.object(handlers, "_notice", new_callable=mock.AsyncMock) as notice, \
             mock.patch.object(handlers, "check_achievements") as achievements:
            asyncio.run(handlers._handle_finish_book(ctx, args, {"response": "Finished!"}, None))
        notice.assert_awaited_once()
        self.assertIn("already marked as finished", notice.await_args.args[1])
        achievements.assert_not_called()

    def test_add_book_does_not_no_op_on_other_guild_row(self):
        uid, name = self.alice
        self.assertTrue(
            action_add_book(uid, name, self.guild_a, {"title": "Neuromancer", "author": "William Gibson"})
        )
        self.assertTrue(
            action_add_book(uid, name, self.guild_b, {"title": "Neuromancer", "author": "William Gibson"})
        )
        self.assertEqual(
            [b["title"] for b in query_shelf(uid, "wishlist", self.guild_a)],
            ["Neuromancer"],
        )
        self.assertEqual(
            [b["title"] for b in query_shelf(uid, "wishlist", self.guild_b)],
            ["Neuromancer"],
        )
        self.assertEqual(
            {(guild, status) for guild, status, _raw in self._rows("Neuromancer")},
            {(self.guild_a, "wishlist"), (self.guild_b, "wishlist")},
        )

    def test_add_book_and_add_to_wishlist_share_cap(self):
        uid, name = self.alice
        for i in range(LIST_CAP):
            ok, status = action_wishlist_add(uid, name, self.guild_a, {"title": f"Book {i}"})
            self.assertTrue(ok, msg=status)
            self.assertEqual(status, "added")
        self.assertFalse(action_add_book(uid, name, self.guild_a, {"title": "Overflow"}))
        self.assertFalse(action_add_to_wishlist(uid, name, self.guild_a, {"title": "Overflow"}))
        self.assertEqual(len(query_shelf(uid, "wishlist", self.guild_a)), LIST_CAP)
        # Other guild is unaffected by the cap.
        self.assertTrue(action_add_book(uid, name, self.guild_b, {"title": "Overflow"}))
        self.assertEqual(
            [b["title"] for b in query_shelf(uid, "wishlist", self.guild_b)],
            ["Overflow"],
        )

    def test_handle_add_book_full_uses_wishlist_notice(self):
        import handlers

        uid, name = self.alice
        for i in range(LIST_CAP):
            action_wishlist_add(uid, name, self.guild_a, {"title": f"Book {i}"})

        notices = []

        async def fake_notice(ctx, text, title=None):
            notices.append((text, title))

        ctx = handlers.RequestContext(
            message=mock.Mock(),
            guild_id=self.guild_a,
            author_id=uid,
            display_name=name,
            reply_channel=mock.Mock(),
        )
        with mock.patch.object(handlers, "_notice", fake_notice):
            asyncio.run(
                handlers._handle_simple(
                    ctx,
                    {"title": "Overflow"},
                    {"action": "add_book", "response": "Added Overflow to your shelf."},
                    None,
                )
            )
        self.assertEqual(len(notices), 1)
        text, title = notices[0]
        self.assertIn("wishlist is full", text)
        self.assertIn("Overflow", text)
        self.assertEqual(title, "✨ Wishlist")
        self.assertEqual(len(query_shelf(uid, "wishlist", self.guild_a)), LIST_CAP)


if __name__ == "__main__":
    unittest.main()
