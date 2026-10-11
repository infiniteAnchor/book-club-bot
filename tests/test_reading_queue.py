"""CI-safe coverage for private reading queues and combined next-book choices."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
from actions import query_shelf
from actions_queue import (
    LIST_CAP,
    action_queue_add,
    action_queue_list,
    action_queue_remove,
    action_save_book_to,
    action_start_from_choice,
    action_wishlist_add,
    action_wishlist_remove,
    list_next_book_choices,
)


class ReadingQueueTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmpdir.name, "bookclub.db")
        self._db_patch = mock.patch.object(database, "DB_PATH", self.db_path)
        self._db_patch.start()
        database.init_db()
        self.guild = "guild-1"
        self.alice = ("alice", "Alice")
        self.bob = ("bob", "Bob")

    def tearDown(self):
        self._db_patch.stop()
        self._tmpdir.cleanup()

    def test_queue_add_list_remove_and_dedupe(self):
        ok, status = action_queue_add(
            self.alice[0], self.alice[1], self.guild, {"title": "Dune", "author": "Frank Herbert"}
        )
        self.assertTrue(ok)
        self.assertEqual(status, "added")

        ok, status = action_queue_add(
            self.alice[0], self.alice[1], self.guild, {"title": "Dune", "author": "Frank Herbert"}
        )
        self.assertTrue(ok)
        self.assertEqual(status, "already")

        items = action_queue_list(self.alice[0], self.guild)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Dune")
        self.assertEqual(items[0]["list"], "queue")

        ok, status = action_queue_remove(
            self.alice[0], self.alice[1], self.guild, {"title": "Dune"}
        )
        self.assertTrue(ok)
        self.assertEqual(status, "removed")
        self.assertEqual(action_queue_list(self.alice[0], self.guild), [])

    def test_queue_is_private_to_member_and_guild(self):
        action_queue_add(
            self.alice[0], self.alice[1], self.guild, {"title": "Neuromancer"}
        )
        self.assertEqual(action_queue_list(self.bob[0], self.guild), [])
        self.assertEqual(action_queue_list(self.alice[0], "other-guild"), [])
        # Shelf query must not expose the private queue.
        self.assertEqual(query_shelf(self.alice[0], "wishlist", self.guild), [])
        self.assertEqual(query_shelf(self.alice[0], "reading", self.guild), [])

    def test_wishlist_remove_and_save_destinations(self):
        ok, status = action_save_book_to(
            "wishlist",
            self.alice[0],
            self.alice[1],
            self.guild,
            {"title": "The Left Hand of Darkness"},
            source="nomination",
        )
        self.assertTrue(ok)
        self.assertEqual(status, "added")
        shelf = query_shelf(self.alice[0], "wishlist", self.guild)
        self.assertEqual([b["title"] for b in shelf], ["The Left Hand of Darkness"])

        ok, status = action_wishlist_remove(
            self.alice[0],
            self.alice[1],
            self.guild,
            {"title": "The Left Hand of Darkness"},
        )
        self.assertTrue(ok)
        self.assertEqual(status, "removed")
        self.assertEqual(query_shelf(self.alice[0], "wishlist", self.guild), [])

        ok, status = action_save_book_to(
            "queue",
            self.alice[0],
            self.alice[1],
            self.guild,
            {"title": "Hyperion"},
            source="recommendation",
        )
        self.assertTrue(ok)
        self.assertEqual(status, "added")
        self.assertEqual(action_queue_list(self.alice[0], self.guild)[0]["source"], "recommendation")

    def test_combined_choices_and_start_from_queue(self):
        action_queue_add(
            self.alice[0], self.alice[1], self.guild, {"title": "Queue Book"}
        )
        action_wishlist_add(
            self.alice[0], self.alice[1], self.guild, {"title": "Wishlist Book"}
        )
        choices = list_next_book_choices(self.alice[0], self.guild)
        self.assertEqual(
            [(c["list"], c["title"]) for c in choices],
            [("queue", "Queue Book"), ("wishlist", "Wishlist Book")],
        )
        self.assertTrue(choices[0]["label"].startswith("Queue ·"))
        self.assertTrue(choices[1]["label"].startswith("Wishlist ·"))

        ok = action_start_from_choice(
            self.alice[0],
            self.alice[1],
            self.guild,
            list_name="queue",
            title="Queue Book",
        )
        self.assertTrue(ok)
        self.assertEqual(action_queue_list(self.alice[0], self.guild), [])
        reading = query_shelf(self.alice[0], "reading", self.guild)
        self.assertEqual([b["title"] for b in reading], ["Queue Book"])

    def test_start_from_wishlist_promotes_shelf(self):
        action_wishlist_add(
            self.alice[0], self.alice[1], self.guild, {"title": "Shelf Book"}
        )
        ok = action_start_from_choice(
            self.alice[0],
            self.alice[1],
            self.guild,
            list_name="wishlist",
            title="Shelf Book",
        )
        self.assertTrue(ok)
        self.assertEqual(query_shelf(self.alice[0], "wishlist", self.guild), [])
        self.assertEqual(
            [b["title"] for b in query_shelf(self.alice[0], "reading", self.guild)],
            ["Shelf Book"],
        )

    def test_queue_cap(self):
        for i in range(LIST_CAP):
            ok, status = action_queue_add(
                self.alice[0],
                self.alice[1],
                self.guild,
                {"title": f"Book {i}"},
            )
            self.assertTrue(ok, msg=status)
            self.assertEqual(status, "added")
        ok, status = action_queue_add(
            self.alice[0], self.alice[1], self.guild, {"title": "Overflow"}
        )
        self.assertFalse(ok)
        self.assertEqual(status, "full")
        # Starting an existing title still reports already, not full.
        ok, status = action_queue_add(
            self.alice[0], self.alice[1], self.guild, {"title": "Book 0"}
        )
        self.assertTrue(ok)
        self.assertEqual(status, "already")

    def test_queue_list_public_ack_does_not_include_titles(self):
        import asyncio
        import handlers
        from sending import QUEUE_LIST_PUBLIC_ACK
        from views_queue import RevealPrivateListView, private_queue_payload

        owner_id = "1001"
        action_queue_add(
            owner_id, "Alice", self.guild, {"title": "Dune", "author": "Frank Herbert"}
        )
        captured = []

        async def fake_send(_channel, embeds, **kwargs):
            captured.append((embeds[0].description, kwargs.get("view")))

        ctx = handlers.RequestContext(
            message=mock.Mock(),
            guild_id=self.guild,
            author_id=owner_id,
            display_name="Alice",
            reply_channel=mock.Mock(),
        )
        with mock.patch.object(handlers, "send_embeds", fake_send):
            asyncio.run(handlers._handle_queue_list(
                ctx, {},
                {"action": "queue_list", "response": "Your private queue: Dune by Frank Herbert"},
                None,
            ))
        self.assertEqual(len(captured), 1)
        description, view = captured[0]
        self.assertEqual(description, QUEUE_LIST_PUBLIC_ACK)
        self.assertNotIn("Dune", description)
        self.assertIsInstance(view, RevealPrivateListView)

        embed, manage = private_queue_payload(owner_id, self.guild)
        self.assertIn("Dune", embed.description or "")
        self.assertIsNotNone(manage)
        llm_src = open("llm.py").read()
        self.assertIn("Do not list book titles", llm_src)


if __name__ == "__main__":
    unittest.main()
