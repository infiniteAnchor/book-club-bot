"""Goodreads finished-history CSV import."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
from actions import (
    action_dnf_book,
    action_finish_book,
    action_rate_book,
    action_start_book,
    action_update_progress,
    get_user_reading_history,
    query_shelf,
)
from actions_engagement import (
    check_achievements,
    get_admin_summary,
    get_club_history,
    get_leaderboard_stats,
    get_streaks,
    get_weekly_activity,
    get_year_activity,
)
from actions_import import (
    MAX_READ_ROWS,
    apply_goodreads_import,
    existing_shelf_titles,
    parse_goodreads_csv,
)
from commands import COMMAND_DESCRIPTIONS, register, slash_import
from database import connect
from views_import import GoodreadsImportView, IMPORT_GUEST_MESSAGE, IMPORT_TRACKING_MESSAGE


FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _read_fixture(name: str) -> str:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return handle.read()


class GoodreadsImportTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmpdir.name, "bookclub.db")
        self._db_patch = mock.patch.object(database, "DB_PATH", self.db_path)
        self._db_patch.start()
        database.init_db()
        self.guild = "guild-1"
        self.user = ("alice", "Alice")
        action_start_book(self.user[0], self.user[1], self.guild, {"title": "Already Finished"})
        self.assertTrue(
            action_finish_book(self.user[0], self.user[1], self.guild, {"title": "Already Finished"})
        )
        self.assertTrue(
            action_rate_book(
                self.user[0],
                self.user[1],
                {"title": "Already Finished", "score": 6},
                self.guild,
            )
        )

    def tearDown(self):
        self._db_patch.stop()
        self._tmpdir.cleanup()

    def _preview(self):
        return parse_goodreads_csv(
            _read_fixture("goodreads_library_export.csv"),
            existing_shelf_titles=existing_shelf_titles(self.user[0], self.guild),
        )

    def _apply(self, preview=None):
        preview = preview or self._preview()
        with mock.patch("actions.lookup_google_books", side_effect=AssertionError("Google Books must not run")):
            return apply_goodreads_import(
                self.user[0], self.user[1], self.guild, preview.to_add
            )

    def test_non_goodreads_header_is_rejected(self):
        preview = parse_goodreads_csv(_read_fixture("not_goodreads.csv"))
        self.assertFalse(preview.ok)
        self.assertEqual(preview.error, "not_goodreads")

    def test_parse_counts_and_only_read_rows(self):
        preview = self._preview()
        self.assertTrue(preview.ok)
        titles = [row.title for row in preview.to_add]
        self.assertEqual(
            titles,
            [
                "The Left Hand of Darkness",
                "Zero Stars",
                "Date Added Only",
                "Recent Finish",
            ],
        )
        self.assertEqual(preview.already_on_shelf, 1)
        self.assertEqual(preview.to_read_ignored, 1)
        self.assertEqual(preview.currently_reading_ignored, 1)
        self.assertEqual(preview.other_ignored, 1)
        self.assertEqual(preview.blank_skipped, 1)
        left_hand = preview.to_add[0]
        self.assertEqual(left_hand.author, "Ursula K. Le Guin")
        self.assertEqual(left_hand.score, 10)
        self.assertIsNotNone(left_hand.read_at)
        self.assertTrue(str(left_hand.read_at).startswith("2020-01-15"))
        zero = next(row for row in preview.to_add if row.title == "Zero Stars")
        self.assertIsNone(zero.score)
        added_only = next(row for row in preview.to_add if row.title == "Date Added Only")
        self.assertEqual(added_only.score, 4)
        self.assertIsNotNone(added_only.read_at)
        self.assertTrue(str(added_only.read_at).startswith("2022-06-06"))
        recent = next(row for row in preview.to_add if row.title == "Recent Finish")
        self.assertEqual(recent.score, 2)

    def test_apply_writes_personal_history_not_club_surfaces(self):
        result = self._apply()
        self.assertEqual(result.added, 4)
        self.assertEqual(result.ratings_added, 3)
        self.assertEqual(result.already_on_shelf, 0)

        personal = query_shelf(self.user[0], "finished", self.guild)
        titles = {row["title"] for row in personal}
        self.assertIn("The Left Hand of Darkness", titles)
        self.assertIn("Zero Stars", titles)
        self.assertIn("Date Added Only", titles)
        self.assertIn("Recent Finish", titles)
        self.assertIn("Already Finished", titles)

        history = get_user_reading_history(self.user[0], self.guild)
        rated = {row["title"]: row["rating"] for row in history["finished"]}
        self.assertEqual(rated["The Left Hand of Darkness"], 10)
        self.assertEqual(rated["Date Added Only"], 4)
        self.assertEqual(rated["Recent Finish"], 2)
        self.assertIsNone(rated["Zero Stars"])
        self.assertEqual(rated["Already Finished"], 6)

        self.assertEqual(get_club_history(self.guild)[0]["title"], "Already Finished")
        self.assertEqual(len(get_club_history(self.guild)), 1)
        stats = get_leaderboard_stats(self.guild)
        self.assertEqual(len(stats), 1)
        self.assertEqual(stats[0]["books_finished"], 1)
        self.assertEqual(get_admin_summary(self.guild)["total_finished"], 1)
        recap_titles = {row["title"] for row in get_weekly_activity(self.guild)["finished"]}
        self.assertIn("Already Finished", recap_titles)
        self.assertNotIn("Recent Finish", recap_titles)
        year_titles = {row["title"] for row in get_year_activity(self.guild)["finished"]}
        self.assertEqual(year_titles, {"Already Finished"})
        conn = connect()
        try:
            achievement_count = conn.execute("SELECT COUNT(*) FROM achievements").fetchone()[0]
            rows = conn.execute(
                """SELECT b.title, rp.source, rp.last_activity_at
                   FROM reading_progress rp
                   JOIN books b ON b.book_id = rp.book_id
                   WHERE rp.source = 'goodreads'
                   ORDER BY b.title"""
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(achievement_count, 0)
        earned = {row["type"] for row in check_achievements(self.user[0], self.user[1], self.guild)}
        self.assertNotIn("five_books", earned)
        self.assertNotIn("generous_soul", earned)
        self.assertNotIn("harsh_critic", earned)
        # Discord finish in setUp counts; imported rows have null last_activity_at.
        self.assertEqual(get_streaks(self.user[0], self.guild)["current"], 1)
        self.assertEqual(len(rows), 4)
        for _title, source, last_activity in rows:
            self.assertEqual(source, "goodreads")
            self.assertIsNone(last_activity)
        rating_rows = connect()
        try:
            sourced = rating_rows.execute(
                """SELECT b.title, r.source, r.rated_at
                   FROM ratings r
                   JOIN books b ON b.book_id = r.book_id
                   JOIN users u ON u.user_id = r.user_id
                   WHERE u.discord_id = ? AND r.source = 'goodreads'
                   ORDER BY b.title""",
                (self.user[0],),
            ).fetchall()
        finally:
            rating_rows.close()
        titles = {row[0] for row in sourced}
        self.assertEqual(titles, {"Date Added Only", "Recent Finish", "The Left Hand of Darkness"})
        left_hand = next(row for row in sourced if row[0] == "The Left Hand of Darkness")
        self.assertTrue(str(left_hand[2]).startswith("2020-01-15"))

    def test_second_apply_is_idempotent_and_does_not_overwrite_rating(self):
        self._apply()
        preview = self._preview()
        self.assertEqual(preview.to_add, [])
        self.assertEqual(preview.already_on_shelf, 5)
        result = self._apply(preview)
        self.assertEqual(result.added, 0)
        history = get_user_reading_history(self.user[0], self.guild)
        rated = {row["title"]: row["rating"] for row in history["finished"]}
        self.assertEqual(rated["Already Finished"], 6)

    def test_read_row_cap(self):
        header = "Title,Exclusive Shelf,My Rating,Date Read,Date Added,Author"
        lines = [header]
        for i in range(MAX_READ_ROWS + 3):
            lines.append(f"Book {i},read,1,2020/01/01,2020/01/01,Author")
        preview = parse_goodreads_csv("\n".join(lines))
        self.assertTrue(preview.truncated)
        self.assertEqual(len(preview.to_add), MAX_READ_ROWS)

    def test_slash_import_is_registered(self):
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
        self.assertIs(tree.cmds["import"][0], slash_import)
        self.assertEqual(tree.cmds["import"][1], COMMAND_DESCRIPTIONS["import"])

    def test_start_and_progress_after_import_use_a_live_row(self):
        self._apply()
        self.assertTrue(
            action_start_book(self.user[0], self.user[1], self.guild, {"title": "The Left Hand of Darkness"})
        )
        self.assertTrue(
            action_update_progress(
                self.user[0],
                self.user[1],
                self.guild,
                {"title": "The Left Hand of Darkness", "progress_raw": 40, "progress_unit": "percent"},
            )
        )
        reading = query_shelf(self.user[0], "reading", self.guild)
        self.assertEqual(reading[0]["title"], "The Left Hand of Darkness")
        self.assertEqual(reading[0]["progress_raw"], 40)
        conn = connect()
        try:
            rows = conn.execute(
                """SELECT rp.status, rp.source
                   FROM reading_progress rp
                   JOIN books b ON b.book_id = rp.book_id
                   JOIN users u ON u.user_id = rp.user_id
                   WHERE u.discord_id = ? AND b.title = 'The Left Hand of Darkness'
                   ORDER BY rp.progress_id""",
                (self.user[0],),
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(rows, [("finished", "goodreads"), ("reading", None)])

    def test_dnf_after_import_does_not_mutate_goodreads_row(self):
        self._apply()
        ok, title, _author = action_dnf_book(
            self.user[0], self.user[1], self.guild, {"title": "Zero Stars"}
        )
        self.assertTrue(ok)
        self.assertEqual(title, "Zero Stars")
        year = get_year_activity(self.guild)
        self.assertIn("Zero Stars", {row["title"] for row in year["dnf"]})
        conn = connect()
        try:
            rows = conn.execute(
                """SELECT rp.status, rp.source
                   FROM reading_progress rp
                   JOIN books b ON b.book_id = rp.book_id
                   JOIN users u ON u.user_id = rp.user_id
                   WHERE u.discord_id = ? AND b.title = 'Zero Stars'
                   ORDER BY rp.progress_id""",
                (self.user[0],),
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(rows, [("finished", "goodreads"), ("dnf", None)])

    def test_imported_rating_stays_off_club_after_discord_reread(self):
        self._apply()
        self.assertTrue(
            action_finish_book(self.user[0], self.user[1], self.guild, {"title": "Recent Finish"})
        )
        self.assertTrue(
            action_finish_book(
                self.user[0], self.user[1], self.guild, {"title": "The Left Hand of Darkness"}
            )
        )
        recap_ratings = {row["title"] for row in get_weekly_activity(self.guild)["ratings"]}
        self.assertNotIn("Recent Finish", recap_ratings)
        self.assertNotIn("The Left Hand of Darkness", recap_ratings)
        year_ratings = {row["title"] for row in get_year_activity(self.guild)["ratings"]}
        self.assertNotIn("Recent Finish", year_ratings)
        history = get_club_history(self.guild)
        recent = next(row for row in history if row["title"] == "Recent Finish")
        self.assertNotIn("avg_score", recent)
        earned = {row["type"] for row in check_achievements(self.user[0], self.user[1], self.guild)}
        self.assertNotIn("generous_soul", earned)

        self.assertTrue(
            action_rate_book(
                self.user[0],
                self.user[1],
                {"title": "Recent Finish", "score": 8},
                self.guild,
            )
        )
        recap_after = {row["title"]: row["score"] for row in get_weekly_activity(self.guild)["ratings"]}
        self.assertEqual(recap_after["Recent Finish"], 8)
        history_after = next(row for row in get_club_history(self.guild) if row["title"] == "Recent Finish")
        self.assertEqual(history_after["avg_score"], 8.0)

    def test_confirm_view_rechecks_access_and_blocks_double_click(self):
        import asyncio
        from types import SimpleNamespace
        from unittest import mock

        self._apply()
        preview = parse_goodreads_csv(
            "Title,Exclusive Shelf,My Rating,Date Read,Date Added,Author\n"
            "New Import Title,read,3,2020/01/01,2020/01/01,Author\n"
        )
        view = GoodreadsImportView(
            guild_id=self.guild,
            owner_id=1,
            display_name="Alice",
            rows=preview.to_add,
        )

        class FakeResponse:
            def __init__(self):
                self.messages = []
                self.deferred = False

            async def send_message(self, content=None, **kwargs):
                self.messages.append(content)

            async def defer(self):
                self.deferred = True

        class FakeInteraction:
            def __init__(self, user_id):
                self.user = SimpleNamespace(id=user_id)
                self.response = FakeResponse()
                self.edits = []

            async def edit_original_response(self, **kwargs):
                self.edits.append(kwargs)

        other = FakeInteraction(99)
        self.assertFalse(asyncio.run(view.interaction_check(other)))  # type: ignore[arg-type]
        self.assertIn("Only the person who ran `/import`", other.response.messages[0])

        guest = FakeInteraction(1)
        with mock.patch("views_import.get_access_tier", return_value="guest"):
            self.assertFalse(asyncio.run(view.interaction_check(guest)))  # type: ignore[arg-type]
        self.assertEqual(guest.response.messages[0], IMPORT_GUEST_MESSAGE)

        tracking = FakeInteraction(1)
        with mock.patch("views_import.get_access_tier", return_value="member"):
            with mock.patch("views_import.get_guild_settings", return_value={"member_opt_in_enabled": True}):
                with mock.patch("views_import.has_tracking", return_value=False):
                    self.assertFalse(asyncio.run(view.interaction_check(tracking)))  # type: ignore[arg-type]
        self.assertEqual(tracking.response.messages[0], IMPORT_TRACKING_MESSAGE)

        first = FakeInteraction(1)
        second = FakeInteraction(1)
        asyncio.run(view.confirm_import.callback(first))  # type: ignore[arg-type]
        self.assertTrue(view._applied)
        self.assertEqual(len(first.edits), 1)
        asyncio.run(view.confirm_import.callback(second))  # type: ignore[arg-type]
        self.assertIn("already ran", second.response.messages[0])
        self.assertEqual(len(second.edits), 0)


if __name__ == "__main__":
    unittest.main()
