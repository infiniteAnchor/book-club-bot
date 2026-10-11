"""Offline coverage for enrollment buttons and the admin roles dashboard."""
import asyncio
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord

import database
import views_enrollment
from actions_social import describe_participation
from views import AdminDashboardView, EnrollmentView, build_admin_dashboard_embed


class FakeResponse:
    def __init__(self):
        self.sent = None

    async def send_message(self, content, ephemeral=False):
        self.sent = content


class FakeInteraction:
    def __init__(self, member, guild):
        self.user = member
        self.guild = guild
        self.guild_id = guild.id
        self.message = None
        self.response = FakeResponse()


class EnrollmentViewTests(unittest.TestCase):
    def test_persistent_custom_ids_are_the_new_tier_set(self):
        view = EnrollmentView()
        self.assertIsNone(view.timeout)
        self.assertEqual(
            [item.custom_id for item in view.children],
            ["enroll_tracking", "enroll_updates", "enroll_club", "enroll_chat", "enroll_nudges"],
        )

    def test_tracking_button_goes_through_set_participation(self):
        captured = {}

        async def fake_set(member, **kwargs):
            captured["kwargs"] = kwargs
            captured["member"] = member
            return {"tracking": True, "updates": False, "club": False, "dm_nudges": False}

        member = mock.Mock(spec=discord.Member)
        member.id = 10
        member.display_name = "Bob"
        guild = mock.Mock()
        guild.id = 500
        interaction = FakeInteraction(member, guild)
        view = EnrollmentView()
        with mock.patch.object(views_enrollment, "set_participation", fake_set), \
             mock.patch.object(views_enrollment, "get_access_tier", return_value="member"):
            asyncio.run(view.enroll_tracking.callback(interaction))
        self.assertEqual(captured["kwargs"], {"tracking": True, "updates": False, "club": False})
        self.assertIs(captured["member"], member)
        self.assertIn("**Tracking**", interaction.response.sent)

    def test_nudge_toggle_flips_current_preference(self):
        captured = {}

        async def fake_set(member, **kwargs):
            captured["kwargs"] = kwargs
            return {"tracking": True, "updates": False, "club": False, "dm_nudges": True}

        member = mock.Mock(spec=discord.Member)
        member.id = 11
        member.display_name = "Ann"
        guild = mock.Mock()
        guild.id = 500
        interaction = FakeInteraction(member, guild)
        view = EnrollmentView()
        with mock.patch.object(views_enrollment, "set_participation", fake_set), \
             mock.patch.object(views_enrollment, "get_access_tier", return_value="member"), \
             mock.patch.object(views_enrollment, "get_member_feature_preferences",
                               return_value={"dm_nudges": False}):
            asyncio.run(view.enroll_nudges.callback(interaction))
        self.assertEqual(captured["kwargs"], {"dm_nudges": True})


class DashboardRolesTests(unittest.TestCase):
    def test_overview_includes_roles_page(self):
        view = AdminDashboardView("500", 1)
        labels = [item.label for item in view.children if hasattr(item, "label")]
        self.assertIn("Roles", labels)

    def test_roles_page_always_offers_sync_and_adopt(self):
        view = AdminDashboardView("500", 1)
        view.page = "roles"
        view.render()
        labels = [item.label for item in view.children if hasattr(item, "label")]
        self.assertIn("Sync roles", labels)
        self.assertIn("Adopt existing role", labels)
        self.assertNotIn("Grant admin", labels)

    def test_selected_member_unlocks_tier_and_admin_controls(self):
        view = AdminDashboardView("500", 1)
        view.page = "roles"
        view.selected_member = mock.Mock()
        view.render()
        labels = [item.label for item in view.children if hasattr(item, "label")]
        for expected in ("Tracking", "Updates", "Club", "Chat only", "Toggle nudges", "Grant admin", "Remove admin"):
            self.assertIn(expected, labels)

    def test_adopt_page_has_key_and_role_selects(self):
        view = AdminDashboardView("500", 1)
        view.page = "adopt"
        view.render()
        kinds = [type(item).__name__ for item in view.children]
        self.assertIn("Select", kinds)
        self.assertIn("RoleSelect", kinds)
        labels = [item.label for item in view.children if hasattr(item, "label")]
        self.assertIn("Adopt", labels)

    def test_roles_embed_lists_managed_roles(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        patcher = mock.patch.object(database, "DB_PATH", os.path.join(tmpdir.name, "bookclub.db"))
        patcher.start()
        self.addCleanup(patcher.stop)
        database.init_db()
        embed = build_admin_dashboard_embed("500", "roles")
        self.assertEqual(embed.title, "👥 Roles")
        self.assertIn("Book Club · Tracking", embed.description)
        self.assertIn("not registered", embed.description)

    def test_describe_participation_names_the_highest_tier(self):
        self.assertIn(
            "**Club**",
            describe_participation({"tracking": True, "updates": True, "club": True, "dm_nudges": False}),
        )
        self.assertIn("**off**", describe_participation({"tracking": False, "updates": False, "club": False, "dm_nudges": False}))


if __name__ == "__main__":
    unittest.main()
