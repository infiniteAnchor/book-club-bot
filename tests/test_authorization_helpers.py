"""Offline coverage for the enrollment-authorization helpers and membership intent.

Covers the DB-level tier helpers (has_tracking / has_club) and the
manage_membership handler's operation/tier → preference mapping, including the
cumulative cascade behavior inherited from set_member_feature_preferences.
"""
import asyncio
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
import discord
from actions import get_or_create_user
import handlers
from actions_social import (
    get_single_participation_guild,
    has_club,
    has_tracking,
    set_member_feature_preferences,
    get_member_feature_preferences,
)


def make_ctx(guild_id: str | None = "500", author_id: str = "1001",
             display_name: str = "Reader") -> handlers.RequestContext:
    message = mock.Mock(spec=discord.Message)
    message.guild = mock.Mock() if guild_id else None
    message.author = mock.Mock(id=int(author_id))
    return handlers.RequestContext(
        message=message,
        guild_id=guild_id,
        author_id=author_id,
        display_name=display_name,
        reply_channel=mock.Mock(),
    )


class AuthorizationHelpersTestBase(unittest.TestCase):
    def setUp(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        patcher = mock.patch.object(database, "DB_PATH", os.path.join(tmpdir.name, "bookclub.db"))
        patcher.start()
        self.addCleanup(patcher.stop)
        database.init_db()


class HasTierHelperTests(AuthorizationHelpersTestBase):
    GUILD = "500"

    def test_falsy_guild_is_false(self):
        set_member_feature_preferences("1", "Reader", self.GUILD, tracking=True)
        self.assertFalse(has_tracking("1", ""))
        self.assertFalse(has_club("1", ""))

    def test_absent_user_row_is_false(self):
        self.assertFalse(has_tracking("404", self.GUILD))
        self.assertFalse(has_club("404", self.GUILD))

    def test_user_without_tier_rows_is_false(self):
        conn = database.connect()
        get_or_create_user(conn, "2", "Quiet")
        conn.commit()
        conn.close()
        self.assertFalse(has_tracking("2", self.GUILD))
        self.assertFalse(has_club("2", self.GUILD))

    def test_tracking_grant_visible_only_in_has_tracking(self):
        set_member_feature_preferences("3", "Tracker", self.GUILD, tracking=True)
        self.assertTrue(has_tracking("3", self.GUILD))
        self.assertFalse(has_club("3", self.GUILD))

    def test_tiers_are_scoped_per_guild(self):
        set_member_feature_preferences("5", "Nomad", "501", tracking=True)
        self.assertFalse(has_tracking("5", self.GUILD))
        self.assertTrue(has_tracking("5", "501"))

    def test_opt_out_clears_the_helper(self):
        set_member_feature_preferences("6", "Leaver", self.GUILD, tracking=True)
        set_member_feature_preferences("6", "Leaver", self.GUILD, tracking=False)
        self.assertFalse(has_tracking("6", self.GUILD))

    def test_single_participation_guild_uses_the_matching_user(self):
        set_member_feature_preferences("7", "DM Reader", self.GUILD, tracking=True)
        self.assertEqual(get_single_participation_guild("7"), self.GUILD)

    def test_single_participation_guild_requires_exactly_one_guild(self):
        set_member_feature_preferences("8", "Multi Guild", self.GUILD, tracking=True)
        set_member_feature_preferences("8", "Multi Guild", "501", tracking=True)
        self.assertIsNone(get_single_participation_guild("8"))


class ManageMembershipHandlerTests(AuthorizationHelpersTestBase):
    GUILD = "500"

    def run_handler(self, args, *, guild_id=GUILD):
        captured = {}

        async def fake_send_embeds(_channel, embeds, **_kwargs):
            captured["embed"] = embeds[0]

        async def fake_write(ctx, **kwargs):
            return set_member_feature_preferences(
                ctx.author_id, ctx.display_name, ctx.guild_id, **kwargs,
            )

        ctx = make_ctx(guild_id=guild_id)
        with mock.patch.object(handlers, "send_embeds", fake_send_embeds), \
             mock.patch.object(handlers, "_write_participation", fake_write), \
             mock.patch("actions.get_access_tier", return_value="member"):
            asyncio.run(handlers._handle_manage_membership(ctx, args, {}, None))
        return captured.get("embed")

    def prefs(self, discord_id="1001"):
        return get_member_feature_preferences(discord_id, self.GUILD)

    def test_join_defaults_to_tracking(self):
        self.run_handler({"operation": "join"})
        prefs = self.prefs()
        self.assertTrue(prefs["tracking"])
        self.assertFalse(prefs["updates"])
        self.assertFalse(prefs["club"])
        self.assertFalse(prefs["dm_nudges"])

    def test_join_chat_keeps_only_basic_access(self):
        set_member_feature_preferences("1001", "Reader", self.GUILD, tracking=True)
        self.run_handler({"operation": "join", "tier": "chat"})
        prefs = self.prefs()
        self.assertFalse(prefs["tracking"])
        self.assertFalse(prefs["dm_nudges"])
        self.assertFalse(prefs["updates"])
        self.assertFalse(prefs["club"])

    def test_join_updates_implies_tracking(self):
        self.run_handler({"operation": "join", "tier": "updates"})
        prefs = self.prefs()
        self.assertTrue(prefs["updates"])
        self.assertTrue(prefs["tracking"])
        self.assertFalse(prefs["club"])

    def test_join_club_cascades_to_updates_and_tracking(self):
        self.run_handler({"operation": "join", "tier": "club"})
        prefs = self.prefs()
        self.assertTrue(prefs["club"])
        self.assertTrue(prefs["updates"])
        self.assertTrue(prefs["tracking"])

    def test_selecting_updates_downgrades_from_club(self):
        self.run_handler({"operation": "join", "tier": "club"})
        self.run_handler({"operation": "join", "tier": "updates"})
        prefs = self.prefs()
        self.assertTrue(prefs["tracking"] and prefs["updates"])
        self.assertFalse(prefs["club"])

    def test_selecting_tracking_downgrades_from_updates(self):
        self.run_handler({"operation": "join", "tier": "updates"})
        self.run_handler({"operation": "join", "tier": "tracking"})
        prefs = self.prefs()
        self.assertTrue(prefs["tracking"])
        self.assertFalse(prefs["updates"] or prefs["club"])

    def test_enable_and_disable_nudges(self):
        self.run_handler({"operation": "join"})
        self.run_handler({"operation": "enable_nudges"})
        self.assertTrue(self.prefs()["dm_nudges"])
        self.run_handler({"operation": "disable_nudges"})
        self.assertFalse(self.prefs()["dm_nudges"])
        # Nudge toggles never touch the member's tier.
        self.assertTrue(self.prefs()["tracking"])

    def test_nudges_require_tracking(self):
        self.run_handler({"operation": "join", "tier": "chat"})
        self.run_handler({"operation": "enable_nudges"})
        prefs = self.prefs()
        # The mirror grants the Tracking prerequisite when nudges are enabled.
        self.assertTrue(prefs["dm_nudges"])
        self.assertTrue(prefs["tracking"])

    def test_show_reports_tier_and_nudges_without_changes(self):
        set_member_feature_preferences("1001", "Reader", self.GUILD, updates=True)
        self.run_handler({"operation": "show"})
        prefs = self.prefs()
        self.assertTrue(prefs["updates"] and prefs["tracking"])
        self.assertFalse(prefs["club"] and prefs["dm_nudges"])

    def test_reply_names_the_tier(self):
        set_member_feature_preferences("1001", "Reader", self.GUILD, updates=True)
        embed = self.run_handler({"operation": "show"})
        self.assertIn("**Updates**", embed.description)

    def test_unknown_operation_gets_guidance_reply(self):
        embed = self.run_handler({"operation": "teleport", "tier": "club"})
        self.assertIn("tier", embed.description.lower())

    def test_dm_context_points_at_enrollment_panel(self):
        with mock.patch.object(handlers, "_settings",
                               return_value={"opt_in_welcome_channel": "welcome"}):
            embed = self.run_handler({"operation": "join"}, guild_id=None)
        self.assertIn("enrollment buttons", embed.description)
        self.assertIn("#welcome", embed.description)


class NominateClubGateTests(AuthorizationHelpersTestBase):
    GUILD = "500"

    def test_tracking_only_cannot_nominate(self):
        set_member_feature_preferences("1001", "Reader", self.GUILD, tracking=True)
        notices = []

        async def fake_notice(_ctx, description, **_kwargs):
            notices.append(description)

        with mock.patch.object(handlers, "_notice", fake_notice):
            asyncio.run(handlers._handle_nominate(
                make_ctx(), {"title": "Dune"}, {"response": "ok"}, None,
            ))
        self.assertTrue(any("Book Club tier" in text for text in notices))

    def test_club_member_can_nominate(self):
        from actions_nominations import get_nominations, open_nomination_window

        set_member_feature_preferences(
            "1001", "Reader", self.GUILD, tracking=True, updates=True, club=True,
        )
        open_nomination_window(self.GUILD, "admin", days=1)
        sent = []

        async def fake_send(*_args, **_kwargs):
            sent.append(True)

        with mock.patch.object(handlers, "send_embeds", fake_send):
            asyncio.run(handlers._handle_nominate(
                make_ctx(),
                {"title": "Dune", "author": "Frank Herbert", "reason": "spice"},
                {"response": "Nominated Dune"},
                None,
            ))
        self.assertTrue(sent)
        self.assertEqual(len(get_nominations(self.GUILD)), 1)


class DmMembershipSyncTests(AuthorizationHelpersTestBase):
    GUILD = "500"

    def test_dm_change_resolves_live_member_and_uses_role_sync(self):
        ctx = make_ctx(guild_id=self.GUILD)
        ctx.message.guild = None
        live_member = mock.Mock()
        guild = mock.Mock()
        guild.get_member.return_value = live_member
        client = mock.Mock()
        client.get_guild.return_value = guild
        expected = {"tracking": True, "updates": True, "club": False, "dm_nudges": False}

        async def fake_set(member, **kwargs):
            self.assertIs(member, live_member)
            self.assertEqual(kwargs, {"updates": True})
            return expected

        with mock.patch.object(handlers.jobs, "client", client), \
             mock.patch.object(handlers, "set_participation", fake_set):
            result = asyncio.run(handlers._write_participation(ctx, updates=True))

        self.assertEqual(result, expected)

    def test_dm_change_without_live_member_never_writes_database_only(self):
        ctx = make_ctx(guild_id=self.GUILD)
        ctx.message.guild = None

        with mock.patch.object(handlers.jobs, "client", None), \
             self.assertRaises(handlers.ParticipationRoleError):
            asyncio.run(handlers._write_participation(ctx, club=True))

        self.assertEqual(
            get_member_feature_preferences(ctx.author_id, self.GUILD),
            {"tracking": False, "updates": False, "club": False, "dm_nudges": False},
        )


if __name__ == "__main__":
    unittest.main()
