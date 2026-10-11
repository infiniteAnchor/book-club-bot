"""Offline coverage for member nominations via apply helper, slash, and button."""
import asyncio
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
from actions_nominations import apply_nomination, get_nominations, open_nomination_window
from actions_social import set_member_feature_preferences
from sending import CLUB_REQUIRED_NOMINATE, GUEST_DENIED_MESSAGE, nomination_denied_message
from views_polls import (
    NOMINATION_TITLE_MAX_LENGTH,
    VOTE_BUTTON_LABEL_MAX_LENGTH,
    NominateView,
    VotingView,
    submit_nomination_interaction,
)


GUILD = "500"


class FakeResponse:
    def __init__(self):
        self.sent = None
        self.kwargs = None
        self.modal = None
        self.deferred = False

    async def defer(self, **kwargs):
        self.deferred = True

    async def send_message(self, content=None, **kwargs):
        self.sent = content
        self.kwargs = kwargs

    async def send(self, content=None, **kwargs):
        await self.send_message(content, **kwargs)

    async def send_modal(self, modal):
        self.modal = modal


class NominateApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_patch = mock.patch.object(
            database, "DB_PATH", os.path.join(self.tmpdir.name, "bookclub.db")
        )
        self.db_patch.start()
        self.lookup_patch = mock.patch("actions.lookup_google_books", return_value={})
        self.lookup_patch.start()
        database.init_db()

    def tearDown(self):
        self.lookup_patch.stop()
        self.db_patch.stop()
        self.tmpdir.cleanup()

    def test_apply_submits_then_replaces(self):
        open_nomination_window(GUILD, "admin", days=1)
        self.assertEqual(
            apply_nomination("1", "Ada", GUILD, "Dune", "Frank Herbert", "spice"),
            "ok",
        )
        self.assertEqual(
            apply_nomination(
                "1", "Ada", GUILD, "Piranesi", "Susanna Clarke", "labyrinth",
                replace_if_present=True,
            ),
            "replaced",
        )
        noms = get_nominations(GUILD)
        self.assertEqual(len(noms), 1)
        self.assertEqual(noms[0]["title"], "Piranesi")

    def test_apply_without_replace_flag_stays_already_nominated(self):
        open_nomination_window(GUILD, "admin", days=1)
        apply_nomination("1", "Ada", GUILD, "Dune", None, None)
        self.assertEqual(
            apply_nomination("1", "Ada", GUILD, "Piranesi", None, None),
            "already_nominated",
        )

    def test_apply_without_window(self):
        self.assertEqual(
            apply_nomination("1", "Ada", GUILD, "Dune", None, None),
            "no_window",
        )


class NominateAccessTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_patch = mock.patch.object(
            database, "DB_PATH", os.path.join(self.tmpdir.name, "bookclub.db")
        )
        self.db_patch.start()
        database.init_db()

    def tearDown(self):
        self.db_patch.stop()
        self.tmpdir.cleanup()

    def test_guest_denied(self):
        from actions import get_or_create_user
        from database import connect

        conn = connect()
        try:
            user_id = get_or_create_user(conn, "9", "Guest")
            conn.execute("UPDATE users SET access_tier = 'guest' WHERE user_id = ?", (user_id,))
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(nomination_denied_message("9", GUILD), GUEST_DENIED_MESSAGE)

    def test_tracking_only_denied_when_opt_in_on(self):
        set_member_feature_preferences("8", "Reader", GUILD, tracking=True)
        self.assertEqual(nomination_denied_message("8", GUILD), CLUB_REQUIRED_NOMINATE)

    def test_club_member_allowed(self):
        set_member_feature_preferences(
            "7", "Reader", GUILD, tracking=True, updates=True, club=True,
        )
        self.assertIsNone(nomination_denied_message("7", GUILD))


class NominateViewTests(unittest.TestCase):
    def test_persistent_nominate_button_id(self):
        view = NominateView()
        self.assertIsNone(view.timeout)
        self.assertEqual(
            [item.custom_id for item in view.children],
            ["nominate_open"],
        )

    def test_long_nomination_title_is_truncated_only_on_vote_button(self):
        title = "T" * NOMINATION_TITLE_MAX_LENGTH
        view = VotingView(
            GUILD,
            1,
            [{"nomination_id": 2, "title": title}],
            timeout=None,
        )
        self.assertEqual(len(view.children[0].label), VOTE_BUTTON_LABEL_MAX_LENGTH)
        self.assertTrue(view.children[0].label.endswith("…"))
        self.assertEqual(view.nominations[2]["title"], title)


class NominateInteractionTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_patch = mock.patch.object(
            database, "DB_PATH", os.path.join(self.tmpdir.name, "bookclub.db")
        )
        self.db_patch.start()
        self.lookup_patch = mock.patch("actions.lookup_google_books", return_value={})
        self.lookup_patch.start()
        database.init_db()
        set_member_feature_preferences(
            "11", "Ada", GUILD, tracking=True, updates=True, club=True,
        )

    def tearDown(self):
        self.lookup_patch.stop()
        self.db_patch.stop()
        self.tmpdir.cleanup()

    def _interaction(self):
        user = SimpleNamespace(id=11, display_name="Ada")
        response = FakeResponse()
        followup = FakeResponse()
        interaction = SimpleNamespace(
            guild_id=int(GUILD),
            user=user,
            response=response,
            followup=followup,
        )
        return interaction

    def test_slash_posts_public_embed_when_window_open(self):
        open_nomination_window(GUILD, "admin", days=1)
        interaction = self._interaction()
        asyncio.run(submit_nomination_interaction(interaction, "Dune", "Frank Herbert", "spice"))
        self.assertIsNone(interaction.response.sent)
        self.assertTrue(interaction.response.deferred)
        embed = interaction.followup.kwargs["embed"]
        self.assertIn("Dune", embed.description)
        self.assertIn("Ada", embed.description)
        self.assertFalse(interaction.followup.kwargs.get("ephemeral"))
        self.assertEqual(len(get_nominations(GUILD)), 1)

    def test_slash_ephemeral_when_no_window(self):
        interaction = self._interaction()
        asyncio.run(submit_nomination_interaction(interaction, "Dune", None, None))
        self.assertIn("no nomination window", interaction.followup.sent)
        self.assertTrue(interaction.followup.kwargs["ephemeral"])


if __name__ == "__main__":
    unittest.main()
