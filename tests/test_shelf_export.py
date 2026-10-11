"""Personal shelf CSV export (Goodreads-shaped, for import elsewhere)."""
import asyncio
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
from actions import (
    action_dnf_book,
    action_finish_book,
    action_rate_book,
    action_start_book,
)
from actions_import import (
    EXPORT_FILENAME,
    EXPORT_HEADERS,
    apply_goodreads_import,
    build_shelf_export,
    parse_goodreads_csv,
)
from actions_queue import action_queue_add, action_wishlist_add
from commands import COMMAND_DESCRIPTIONS, register, slash_export
from views_import import (
    EXPORT_GUEST_MESSAGE,
    EXPORT_TRACKING_MESSAGE,
    build_export_embed,
    build_import_preview_embed,
)


def _rows(csv_text: str) -> list[dict[str, str]]:
    preview_headers = [h.strip().lower() for h in csv_text.splitlines()[0].split(",")]
    from io import StringIO
    import csv

    reader = csv.DictReader(StringIO(csv_text))
    assert reader.fieldnames == list(EXPORT_HEADERS)
    assert preview_headers[0] == "title"
    return list(reader)


class ShelfExportTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmpdir.name, "bookclub.db")
        self._db_patch = mock.patch.object(database, "DB_PATH", self.db_path)
        self._db_patch.start()
        database.init_db()
        self.guild = "guild-1"
        self.other_guild = "guild-2"
        self.user = ("alice", "Alice")
        self.other = ("bob", "Bob")

    def tearDown(self):
        self._db_patch.stop()
        self._tmpdir.cleanup()

    def test_empty_shelf(self):
        result = build_shelf_export(self.user[0], self.guild)
        self.assertEqual(result.row_count, 0)
        self.assertEqual(result.csv_text, "")
        embed = build_export_embed(result)
        self.assertIn("Nothing on your shelf", embed.description)

    def test_finished_reading_wishlist_queue_and_dnf(self):
        self.assertTrue(
            action_start_book(self.user[0], self.user[1], self.guild, {"title": "Dune", "author": "Frank Herbert"})
        )
        self.assertTrue(
            action_finish_book(self.user[0], self.user[1], self.guild, {"title": "Dune"})
        )
        self.assertTrue(
            action_rate_book(self.user[0], self.user[1], {"title": "Dune", "score": 9}, self.guild)
        )
        self.assertTrue(
            action_start_book(self.user[0], self.user[1], self.guild, {"title": "Piranesi"})
        )
        ok, _status = action_wishlist_add(
            self.user[0], self.user[1], self.guild, {"title": "The Left Hand of Darkness"}
        )
        self.assertTrue(ok)
        q_ok, q_status = action_queue_add(
            self.user[0], self.user[1], self.guild, {"title": "Secret Queue Book"}
        )
        self.assertTrue(q_ok)
        self.assertEqual(q_status, "added")
        self.assertTrue(
            action_start_book(self.user[0], self.user[1], self.guild, {"title": "Abandoned"})
        )
        dnf_ok, _title, _author = action_dnf_book(
            self.user[0], self.user[1], self.guild, {"title": "Abandoned"}
        )
        self.assertTrue(dnf_ok)

        result = build_shelf_export(self.user[0], self.guild)
        self.assertEqual(result.finished, 1)
        self.assertEqual(result.reading, 1)
        self.assertEqual(result.to_read, 2)
        self.assertEqual(result.dnf, 1)
        self.assertEqual(result.queue_included, 1)
        rows = {row["Title"]: row for row in _rows(result.csv_text)}
        self.assertEqual(rows["Dune"]["Exclusive Shelf"], "read")
        self.assertEqual(rows["Dune"]["My Rating"], "5")
        self.assertEqual(rows["Dune"]["Author"], "Frank Herbert")
        self.assertTrue(rows["Dune"]["Date Read"])
        self.assertEqual(rows["Piranesi"]["Exclusive Shelf"], "currently-reading")
        self.assertEqual(rows["The Left Hand of Darkness"]["Exclusive Shelf"], "to-read")
        self.assertEqual(rows["Secret Queue Book"]["Exclusive Shelf"], "to-read")
        self.assertEqual(rows["Abandoned"]["Exclusive Shelf"], "read")
        self.assertEqual(rows["Abandoned"]["Bookshelves"], "dnf")

    def test_live_row_wins_over_goodreads_duplicate(self):
        apply_goodreads_import(
            self.user[0],
            self.user[1],
            self.guild,
            parse_goodreads_csv(
                "Title,Exclusive Shelf,My Rating,Date Read,Date Added,Author\n"
                "The Left Hand of Darkness,read,5,2020/01/15,2019/12/01,Ursula K. Le Guin\n"
            ).to_add,
        )
        self.assertTrue(
            action_start_book(
                self.user[0],
                self.user[1],
                self.guild,
                {"title": "The Left Hand of Darkness"},
            )
        )
        result = build_shelf_export(self.user[0], self.guild)
        matches = [row for row in _rows(result.csv_text) if row["Title"] == "The Left Hand of Darkness"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["Exclusive Shelf"], "currently-reading")

    def test_guild_and_user_isolation(self):
        self.assertTrue(
            action_finish_book(self.user[0], self.user[1], self.guild, {"title": "Alice Book"})
        )
        self.assertTrue(
            action_finish_book(self.other[0], self.other[1], self.guild, {"title": "Bob Book"})
        )
        self.assertTrue(
            action_finish_book(self.user[0], self.user[1], self.other_guild, {"title": "Other Server"})
        )
        titles = {row["Title"] for row in _rows(build_shelf_export(self.user[0], self.guild).csv_text)}
        self.assertEqual(titles, {"Alice Book"})

    def test_export_roundtrips_through_goodreads_parser(self):
        self.assertTrue(
            action_finish_book(self.user[0], self.user[1], self.guild, {"title": "Dune", "author": "Frank Herbert"})
        )
        self.assertTrue(
            action_rate_book(self.user[0], self.user[1], {"title": "Dune", "score": 8}, self.guild)
        )
        csv_text = build_shelf_export(self.user[0], self.guild).csv_text
        preview = parse_goodreads_csv(csv_text)
        self.assertTrue(preview.ok)
        self.assertEqual(len(preview.to_add), 1)
        self.assertEqual(preview.to_add[0].title, "Dune")
        self.assertEqual(preview.to_add[0].score, 8)

    def test_queue_already_on_wishlist_is_one_row(self):
        ok, _status = action_wishlist_add(
            self.user[0], self.user[1], self.guild, {"title": "Shared Title"}
        )
        self.assertTrue(ok)
        q_ok, q_status = action_queue_add(
            self.user[0], self.user[1], self.guild, {"title": "Shared Title"}
        )
        self.assertTrue(q_ok)
        self.assertEqual(q_status, "added")
        result = build_shelf_export(self.user[0], self.guild)
        matches = [row for row in _rows(result.csv_text) if row["Title"] == "Shared Title"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(result.queue_included, 0)

    def test_slash_export_is_registered(self):
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
        self.assertIs(tree.cmds["export"][0], slash_export)
        self.assertEqual(tree.cmds["export"][1], COMMAND_DESCRIPTIONS["export"])
        self.assertLessEqual(len(COMMAND_DESCRIPTIONS["export"]), 100)

    def test_slash_export_is_ephemeral_and_attaches_csv(self):
        self.assertTrue(
            action_finish_book(self.user[0], self.user[1], self.guild, {"title": "Dune"})
        )

        class FakeResponse:
            def __init__(self):
                self.kwargs = None

            async def send_message(self, content=None, **kwargs):
                self.kwargs = kwargs
                if content:
                    self.kwargs["content"] = content

        class FakeInteraction:
            def __init__(self, *, guild_id=None, user_id="alice", guest=False, tracking=True):
                self.guild = SimpleNamespace(id=guild_id) if guild_id else None
                self.guild_id = guild_id
                self.user = SimpleNamespace(id=user_id, display_name="Alice")
                self.response = FakeResponse()
                self._guest = guest
                self._tracking = tracking

        with mock.patch("commands.get_access_tier", return_value="member"):
            with mock.patch("commands.get_guild_settings", return_value={"member_opt_in_enabled": False}):
                interaction = FakeInteraction(guild_id=self.guild)
                asyncio.run(slash_export(interaction))  # type: ignore[arg-type]
        kwargs = interaction.response.kwargs
        self.assertTrue(kwargs["ephemeral"])
        self.assertEqual(kwargs["file"].filename, EXPORT_FILENAME)
        self.assertIn("personal shelf", kwargs["embed"].description)

        dm = FakeInteraction()
        asyncio.run(slash_export(dm))  # type: ignore[arg-type]
        self.assertIn("book-club server", dm.response.kwargs["content"])

        with mock.patch("commands.get_access_tier", return_value="guest"):
            guest = FakeInteraction(guild_id=self.guild)
            asyncio.run(slash_export(guest))  # type: ignore[arg-type]
        self.assertEqual(guest.response.kwargs["content"], EXPORT_GUEST_MESSAGE)

        with mock.patch("commands.get_access_tier", return_value="member"):
            with mock.patch("commands.get_guild_settings", return_value={"member_opt_in_enabled": True}):
                with mock.patch("commands.has_tracking", return_value=False):
                    tracking = FakeInteraction(guild_id=self.guild)
                    asyncio.run(slash_export(tracking))  # type: ignore[arg-type]
        self.assertEqual(tracking.response.kwargs["content"], EXPORT_TRACKING_MESSAGE)


    def test_formula_leading_cells_are_escaped_and_roundtrip(self):
        self.assertTrue(
            action_finish_book(self.user[0], self.user[1], self.guild, {"title": "=1+1", "author": "+1+1"})
        )
        self.assertTrue(
            action_finish_book(self.user[0], self.user[1], self.guild, {"title": "@SUM(A1)", "author": "-Anon"})
        )
        csv_text = build_shelf_export(self.user[0], self.guild).csv_text
        rows = {row["Title"]: row for row in _rows(csv_text)}
        self.assertEqual(set(rows), {"'=1+1", "'@SUM(A1)"})
        self.assertEqual(rows["'=1+1"]["Author"], "'+1+1")
        self.assertEqual(rows["'@SUM(A1)"]["Author"], "'-Anon")

        preview = parse_goodreads_csv(csv_text)
        self.assertEqual(
            {(row.title, row.author) for row in preview.to_add},
            {("=1+1", "+1+1"), ("@SUM(A1)", "-Anon")},
        )

    def test_reimported_dnf_is_not_a_finish(self):
        self.assertTrue(
            action_start_book(self.user[0], self.user[1], self.guild, {"title": "Abandoned"})
        )
        dnf_ok, _title, _author = action_dnf_book(
            self.user[0], self.user[1], self.guild, {"title": "Abandoned"}
        )
        self.assertTrue(dnf_ok)
        self.assertTrue(
            action_finish_book(self.user[0], self.user[1], self.guild, {"title": "Dune"})
        )
        preview = parse_goodreads_csv(build_shelf_export(self.user[0], self.guild).csv_text)
        self.assertEqual([row.title for row in preview.to_add], ["Dune"])
        self.assertEqual(preview.dnf_ignored, 1)
        self.assertIn("1** DNF", build_import_preview_embed(preview).description)

    def test_chat_export_points_to_slash_command_without_titles(self):
        import handlers

        self.assertTrue(
            action_finish_book(self.user[0], self.user[1], self.guild, {"title": "Dune"})
        )
        captured = []

        async def fake_notice(_channel, description, **kwargs):
            captured.append(description)

        for guild in (object(), None):
            ctx = handlers.RequestContext(
                message=mock.Mock(guild=guild),
                guild_id=self.guild,
                author_id=self.user[0],
                display_name=self.user[1],
                reply_channel=mock.Mock(),
            )
            with mock.patch.object(handlers, "send_notice", fake_notice):
                asyncio.run(handlers._handle_export_shelf(
                    ctx, {}, {"action": "export_shelf", "response": "Here's Dune"}, None,
                ))
        self.assertEqual(len(captured), 2)
        for text in captured:
            self.assertIn("/export", text)
            self.assertNotIn("Dune", text)
        self.assertIn("book-club server", captured[1])
        self.assertIn("export_shelf", handlers.GUEST_BLOCKED_ACTIONS)
        self.assertNotIn("export_shelf", handlers.OPT_IN_EXEMPT_ACTIONS)

if __name__ == "__main__":
    unittest.main()
