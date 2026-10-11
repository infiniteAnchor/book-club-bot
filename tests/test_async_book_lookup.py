"""Google Books enrichment must not block Discord's event loop."""
import asyncio
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import actions
import database
from actions_nominations import apply_nomination, open_nomination_window, replace_nomination, submit_nomination
from actions_queue import action_queue_add
from actions_session import maybe_start_from_poll_winner, start_club_read


class AsyncBookLookupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_patch = mock.patch.object(database, "DB_PATH", os.path.join(self.tmpdir.name, "books.db"))
        self.db_patch.start()
        database.init_db()

    def tearDown(self):
        self.db_patch.stop()
        self.tmpdir.cleanup()

    async def test_lookup_off_loop_and_nested_action_consumes_metadata(self):
        loop_thread = threading.get_ident()
        lookup_started = threading.Event()
        release_lookup = threading.Event()

        def lookup(title, author):
            self.assertNotEqual(threading.get_ident(), loop_thread)
            lookup_started.set()
            self.assertTrue(release_lookup.wait(2))
            return {"author": "Fetched Author", "page_count": 321, "genre": "Fiction"}

        with mock.patch.object(actions, "lookup_google_books", side_effect=lookup) as lookup_mock:
            task = asyncio.create_task(actions.run_book_action(
                action_queue_add, "user", "User", "guild", {"title": "Dune"},
            ))
            self.assertTrue(await asyncio.to_thread(lookup_started.wait, 2))
            # This coroutine still runs while the blocking HTTP stub is paused.
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release_lookup.set()
            self.assertEqual(await task, (True, "added"))
            self.assertEqual(lookup_mock.call_count, 1)

        conn = database.connect()
        try:
            row = conn.execute("SELECT author, page_count, genre FROM books WHERE title = 'Dune'").fetchone()
            self.assertEqual(tuple(row), ("Fetched Author", 321, "Fiction"))
        finally:
            conn.close()

    async def test_existing_book_skips_lookup(self):
        conn = database.connect()
        try:
            conn.execute("INSERT INTO books (title, author) VALUES ('Dune', 'Frank Herbert')")
            conn.commit()
        finally:
            conn.close()
        with mock.patch.object(actions, "lookup_google_books", side_effect=AssertionError("network call")):
            result = await actions.run_book_action(
                action_queue_add, "user", "User", "guild", {"title": "dUnE"},
            )
        self.assertEqual(result, (True, "added"))

    async def test_prefetch_is_scoped_to_one_call(self):
        with mock.patch.object(actions, "lookup_google_books", return_value={"genre": "Novel"}) as lookup:
            await actions.run_book_action(
                action_queue_add, "user", "User", "guild", {"title": "Dune"},
            )
            self.assertIsNone(actions._prefetched_books.get())
            await actions.run_book_action(
                action_queue_add, "user", "User", "guild", {"title": "Foundation"},
            )
        self.assertEqual(lookup.call_count, 2)

    async def test_positional_title_nomination_uses_prefetch(self):
        open_nomination_window("guild", "admin")
        loop_thread = threading.get_ident()

        def lookup(title, author):
            self.assertNotEqual(threading.get_ident(), loop_thread)
            return {"page_count": 220}

        with mock.patch.object(actions, "lookup_google_books", side_effect=lookup) as lookup_mock:
            result = await actions.run_book_action(
                submit_nomination, "user", "User", "guild", "Piranesi", "Susanna Clarke", None,
            )
        self.assertEqual(result, (True, "ok"))
        lookup_mock.assert_called_once_with("Piranesi", "Susanna Clarke")

    async def test_nomination_without_window_rejects_before_lookup(self):
        with mock.patch.object(actions, "lookup_google_books", side_effect=AssertionError("network call")):
            self.assertEqual(
                await actions.run_book_action(
                    submit_nomination, "user", "User", "guild", "Dune", None, None,
                ),
                (False, "no_window"),
            )
            self.assertEqual(
                await actions.run_book_action(
                    apply_nomination, "user", "User", "guild", "Dune", None, None,
                ),
                "no_window",
            )
            self.assertFalse(await actions.run_book_action(
                replace_nomination, "user", "guild", "Dune", None, None,
            ))

    async def test_club_start_rejections_skip_lookup(self):
        with mock.patch.object(actions, "lookup_google_books", return_value={}) as lookup:
            first = await actions.run_book_action(
                start_club_read, "guild", "admin", {"title": "Dune"},
            )
            self.assertTrue(first[0])
            lookup.assert_called_once_with("Dune", None)

            self.assertEqual(
                (await actions.run_book_action(
                    start_club_read, "guild", "admin", {"title": "New book", "target_date": "bad date"},
                ))[1],
                "invalid_date",
            )
            self.assertEqual(
                (await actions.run_book_action(
                    start_club_read, "guild", "admin", {"title": "New book"},
                ))[1],
                "already_active",
            )
            self.assertIsNone(await actions.run_book_action(
                maybe_start_from_poll_winner, "guild", "admin", {"title": "New book"},
            ))
            lookup.assert_called_once_with("Dune", None)
