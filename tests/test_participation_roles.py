"""Offline coverage for the managed participation-role service."""
import asyncio
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
import participation_roles as pr
from actions_social import (
    CLUB_FEATURE,
    DM_NUDGES_FEATURE,
    TRACKING_FEATURE,
    UPDATES_FEATURE,
    set_member_feature_preferences,
)


class FakeRole:
    def __init__(self, name: str, role_id: int, position: int = 0):
        self.name = name
        self.id = role_id
        self.position = position


class FakePermissions:
    manage_roles = True


class FakeTopRole:
    position = 10_000_000


class FakeMe:
    def __init__(self):
        self.guild_permissions = FakePermissions()
        self.top_role = FakeTopRole()


class FakeMember:
    def __init__(self, member_id: int, name: str, guild, roles=(), bot=False):
        self.id = member_id
        self.display_name = name
        self.guild = guild
        self.roles = list(roles)
        self.bot = bot
        self.added = []
        self.removed = []

    async def add_roles(self, *roles, reason=None):
        for role in roles:
            if role not in self.roles:
                self.roles.append(role)
            self.added.append(role)

    async def remove_roles(self, *roles, reason=None):
        for role in roles:
            if role in self.roles:
                self.roles.remove(role)
            self.removed.append(role)


class FakeGuild:
    def __init__(self, guild_id: int = 500):
        self.id = guild_id
        self.me = FakeMe()
        self.chunked = True
        self.members = []
        self.owner_id = 1
        self.owner = None
        self._roles = {}
        self._next_role_id = 9000

    @property
    def roles(self):
        return list(self._roles.values())

    def get_role(self, role_id):
        return self._roles.get(role_id)

    def get_member(self, member_id):
        return next((m for m in self.members if m.id == member_id), None)

    async def chunk(self):
        pass

    async def create_role(self, name=None, colour=None, reason=None):
        self._next_role_id += 1
        role = FakeRole(name, self._next_role_id, position=self._next_role_id)
        self._roles[role.id] = role
        return role


def register_all(guild: FakeGuild) -> dict:
    """Register every managed role directly, bypassing Discord creation."""
    roles = {}
    for key in pr.MANAGED_ROLE_KEYS:
        role = asyncio.run(guild.create_role(name=pr.ROLE_NAMES[key]))
        pr.set_managed_role_id(str(guild.id), key, role.id)
        roles[key] = role
    return roles


def mirrored_prefs(discord_id: str) -> dict:
    conn = database.connect()
    try:
        rows = conn.execute(
            """SELECT p.feature_key, p.enabled FROM member_feature_preferences p
               JOIN users u ON u.user_id = p.user_id WHERE u.discord_id = ?""",
            (discord_id,),
        ).fetchall()
        return dict(rows)
    finally:
        conn.close()


class ParticipationRolesTestBase(unittest.TestCase):
    def setUp(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        self.db_path = os.path.join(tmpdir.name, "bookclub.db")
        patcher = mock.patch.object(database, "DB_PATH", self.db_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        database.init_db()


class RegistryTests(ParticipationRolesTestBase):
    def test_ensure_creates_and_stores_ids_without_adopting_name_matches(self):
        guild = FakeGuild()
        # A pre-existing role with the expected Tracking name must NOT be adopted.
        squatter = FakeRole(pr.ROLE_NAMES[TRACKING_FEATURE], 777)
        guild._roles[777] = squatter

        roles = asyncio.run(pr.ensure_managed_roles(guild))
        registered = pr.get_managed_role_ids(str(guild.id))

        self.assertNotEqual(roles[TRACKING_FEATURE].id, 777)
        self.assertEqual(
            roles[TRACKING_FEATURE].name,
            f"{pr.ROLE_NAMES[TRACKING_FEATURE]} {pr._COLLISION_SUFFIX}",
        )
        self.assertEqual(set(registered), set(pr.MANAGED_ROLE_KEYS))

    def test_deleted_stored_role_is_replaced_and_reregistered(self):
        guild = FakeGuild()
        register_all(guild)
        original_id = pr.get_managed_role_id(str(guild.id), CLUB_FEATURE)
        del guild._roles[int(original_id)]

        roles = asyncio.run(pr.ensure_managed_roles(guild))

        self.assertNotEqual(str(roles[CLUB_FEATURE].id), original_id)
        self.assertEqual(
            pr.get_managed_role_id(str(guild.id), CLUB_FEATURE),
            str(roles[CLUB_FEATURE].id),
        )

    def test_missing_manage_roles_fails_loudly(self):
        guild = FakeGuild()
        guild.me.guild_permissions.manage_roles = False
        with self.assertRaises(pr.ParticipationRoleError):
            asyncio.run(pr.ensure_managed_roles(guild))


class SetParticipationTests(ParticipationRolesTestBase):
    def test_granting_club_grants_whole_chain_on_discord(self):
        guild = FakeGuild()
        roles = register_all(guild)
        member = FakeMember(10, "Bob", guild)
        guild.members.append(member)

        prefs = asyncio.run(pr.set_participation(member, club=True))

        self.assertTrue(prefs["club"] and prefs["updates"] and prefs["tracking"])
        for key in (TRACKING_FEATURE, UPDATES_FEATURE, CLUB_FEATURE):
            self.assertIn(roles[key], member.roles)
        self.assertIn(roles["chat"], member.roles)

    def test_opt_out_removes_tiers_but_keeps_chat(self):
        guild = FakeGuild()
        roles = register_all(guild)
        member = FakeMember(11, "Ann", guild)
        guild.members.append(member)
        asyncio.run(pr.set_participation(member, tracking=True, dm_nudges=True))

        asyncio.run(pr.set_participation(member, tracking=False, dm_nudges=False))

        self.assertNotIn(roles["chat"], member.removed)
        for key in (TRACKING_FEATURE, UPDATES_FEATURE, CLUB_FEATURE, DM_NUDGES_FEATURE):
            self.assertNotIn(roles[key], member.roles)

    def test_dm_nudges_require_tracking(self):
        guild = FakeGuild()
        roles = register_all(guild)
        member = FakeMember(12, "Cid", guild)
        guild.members.append(member)

        prefs = asyncio.run(pr.set_participation(member, dm_nudges=True))

        self.assertTrue(prefs["tracking"] and prefs["dm_nudges"])
        self.assertIn(roles[DM_NUDGES_FEATURE], member.roles)

    def test_failed_discord_update_does_not_grant_database_authorization(self):
        guild = FakeGuild()
        register_all(guild)
        member = FakeMember(13, "Dee", guild)

        async def fail_roles(*_roles, **_kwargs):
            raise RuntimeError("Discord write failed")

        member.add_roles = fail_roles
        with self.assertRaisesRegex(RuntimeError, "Discord write failed"):
            asyncio.run(pr.set_participation(member, club=True))

        self.assertFalse(mirrored_prefs("13"))

    def test_missing_manage_roles_does_not_fall_back_to_database_only(self):
        guild = FakeGuild()
        guild.me.guild_permissions.manage_roles = False
        member = FakeMember(14, "Eli", guild)

        with self.assertRaises(pr.ParticipationRoleError):
            asyncio.run(pr.set_participation(member, tracking=True))

        self.assertFalse(mirrored_prefs("14"))


class ListenerTests(ParticipationRolesTestBase):
    def test_unrelated_role_changes_are_ignored(self):
        guild = FakeGuild()
        register_all(guild)
        outsider_a = asyncio.run(guild.create_role(name="Random Role A"))
        outsider_b = asyncio.run(guild.create_role(name="Random Role B"))
        before = FakeMember(21, "Eve", guild, roles=[outsider_a])
        after = FakeMember(21, "Eve", guild, roles=[outsider_a, outsider_b])
        guild.members.append(after)

        result = asyncio.run(pr.handle_member_update(before, after))

        self.assertIsNone(result)

    def test_manual_tier_change_is_mirrored_to_db_with_repairs(self):
        guild = FakeGuild()
        roles_map = register_all(guild)
        before = FakeMember(22, "Fay", guild)
        # An admin manually grants Club without its prerequisites and without Chat.
        after = FakeMember(22, "Fay", guild, roles=[roles_map[CLUB_FEATURE]])

        asyncio.run(pr.handle_member_update(before, after))

        mirrored = mirrored_prefs("22")
        self.assertTrue(
            mirrored[CLUB_FEATURE] and mirrored[UPDATES_FEATURE] and mirrored[TRACKING_FEATURE]
        )
        # Repairs grant missing prerequisites (and Chat) on Discord too.
        self.assertIn(roles_map[UPDATES_FEATURE], after.roles)
        self.assertIn(roles_map[TRACKING_FEATURE], after.roles)
        self.assertIn(roles_map["chat"], after.roles)

    def test_bot_originated_updates_are_idempotent(self):
        guild = FakeGuild()
        roles_map = register_all(guild)
        member = FakeMember(23, "Gil", guild, roles=[roles_map["chat"]])
        guild.members.append(member)
        asyncio.run(pr.set_participation(member, tracking=True))

        before = FakeMember(23, "Gil", guild, roles=[roles_map["chat"]])
        result = asyncio.run(pr.handle_member_update(before, member))

        # State already normalized: no Discord deltas, no duplicate grants.
        self.assertIsNotNone(result)
        self.assertEqual(result["deltas"], {})
        self.assertEqual(member.added.count(roles_map[TRACKING_FEATURE]), 1)


class BootstrapTests(ParticipationRolesTestBase):
    def test_bootstrap_assigns_chat_and_persisted_tiers_then_marks_complete(self):
        guild = FakeGuild()
        roles = register_all(guild)
        set_member_feature_preferences("30", "Hal", str(guild.id), tracking=True, dm_nudges=True)
        hal = FakeMember(30, "Hal", guild)
        iris = FakeMember(31, "Iris", guild)
        bot_member = FakeMember(99, "Bot", guild, bot=True)
        guild.members.extend([hal, iris, bot_member])
        guild.owner_id = 31

        processed = asyncio.run(pr.bootstrap_guild(guild))

        self.assertEqual(processed, 2)  # bots excluded
        self.assertIn(roles["chat"], hal.roles)
        self.assertIn(roles["chat"], iris.roles)
        self.assertIn(roles[TRACKING_FEATURE], hal.roles)
        self.assertIn(roles[DM_NUDGES_FEATURE], hal.roles)
        self.assertNotIn(roles[TRACKING_FEATURE], iris.roles)
        # The owner received the admin role once.
        self.assertEqual(iris.added.count(roles["admin"]), 1)
        self.assertIsNotNone(pr.get_sync_completed_at(str(guild.id)))

    def test_partial_bootstrap_does_not_mark_complete(self):
        guild = FakeGuild()
        register_all(guild)
        good = FakeMember(32, "Jo", guild)
        bad = FakeMember(33, "Kay", guild)
        guild.members.extend([good, bad])

        async def boom(*_args, **_kwargs):
            raise RuntimeError("fetch failed")

        with mock.patch.object(FakeMember, "add_roles", new=boom):
            with self.assertRaises(pr.ParticipationRoleError):
                asyncio.run(pr.bootstrap_guild(guild))

        self.assertIsNone(pr.get_sync_completed_at(str(guild.id)))

    def test_run_startup_sync_bootstraps_once_then_reconciles(self):
        guild = FakeGuild()
        roles = register_all(guild)
        member = FakeMember(34, "Lou", guild)
        guild.members.append(member)

        first = asyncio.run(pr.run_startup_sync(guild))
        second = asyncio.run(pr.run_startup_sync(guild))

        self.assertEqual(first, "bootstrap")
        self.assertEqual(second, "reconcile")
        self.assertIn(roles["chat"], member.roles)


class ReconcileTests(ParticipationRolesTestBase):
    def test_reconcile_restores_chat_fixes_violations_and_mirrors_db(self):
        guild = FakeGuild()
        roles = register_all(guild)
        # No chat, orphan nudge without tracking.
        orphan = FakeMember(40, "Mel", guild, roles=[roles[DM_NUDGES_FEATURE]])
        # Club granted without prerequisites or chat.
        partial = FakeMember(41, "Ned", guild, roles=[roles[CLUB_FEATURE]])
        guild.members.extend([orphan, partial])

        summary = asyncio.run(pr.reconcile_guild(guild))

        self.assertIn(roles["chat"], orphan.roles)
        self.assertIn(roles["chat"], partial.roles)
        self.assertNotIn(roles[DM_NUDGES_FEATURE], orphan.roles)
        self.assertIn(roles[UPDATES_FEATURE], partial.roles)
        self.assertIn(roles[TRACKING_FEATURE], partial.roles)
        self.assertGreaterEqual(summary["restored_chat"], 2)

        self.assertFalse(mirrored_prefs("40")[DM_NUDGES_FEATURE])
        ned = mirrored_prefs("41")
        self.assertTrue(ned[CLUB_FEATURE] and ned[UPDATES_FEATURE] and ned[TRACKING_FEATURE])

    def test_recreated_roles_restore_membership_from_database_mirror(self):
        guild = FakeGuild()
        roles = register_all(guild)
        member = FakeMember(
            42,
            "Ona",
            guild,
            roles=[
                roles["chat"],
                roles[TRACKING_FEATURE],
                roles[UPDATES_FEATURE],
                roles[CLUB_FEATURE],
                roles[DM_NUDGES_FEATURE],
            ],
        )
        guild.members.append(member)
        set_member_feature_preferences(
            "42", "Ona", str(guild.id), club=True, dm_nudges=True,
        )
        deleted_keys = (CLUB_FEATURE, DM_NUDGES_FEATURE)
        for key in deleted_keys:
            member.roles.remove(roles[key])
            del guild._roles[roles[key].id]

        summary = asyncio.run(pr.reconcile_guild(guild))

        self.assertEqual(set(summary["recreated"]), set(deleted_keys))
        for key in deleted_keys:
            self.assertIn(pr.resolve(guild, key), member.roles)
        prefs = mirrored_prefs("42")
        self.assertTrue(prefs[CLUB_FEATURE] and prefs[DM_NUDGES_FEATURE])


class RestoreOnRejoinTests(ParticipationRolesTestBase):
    def test_rejoin_restores_chat_and_persisted_state(self):
        guild = FakeGuild()
        roles = register_all(guild)
        set_member_feature_preferences("50", "Ora", str(guild.id), updates=True)

        returning = FakeMember(50, "Ora", guild)
        asyncio.run(pr.restore_on_rejoin(returning))

        self.assertIn(roles["chat"], returning.roles)
        self.assertIn(roles[UPDATES_FEATURE], returning.roles)
        self.assertIn(roles[TRACKING_FEATURE], returning.roles)
        self.assertNotIn(roles[DM_NUDGES_FEATURE], returning.roles)


class _StaticPermissions:
    manage_guild = False
    administrator = False


def _plain_member(*, owner: bool, has_admin_role_name: bool):
    class FakeGuildRef:
        owner_id = 1

    class FakeRoleRef:
        def __init__(self, name: str):
            self.name = name

    class Member:
        def __init__(self, member_id: int, guild, role_names, permissions):
            self.id = member_id
            self.guild = guild
            self.roles = [FakeRoleRef(name) for name in role_names]
            self.guild_permissions = permissions

    names = ["Book Club Admin"] if has_admin_role_name else ["Member"]
    return Member(1 if owner else 2, FakeGuildRef(), names, _StaticPermissions())


class AdoptExistingRoleTests(ParticipationRolesTestBase):
    def test_adopt_stores_id_without_renaming_or_deleting(self):
        guild = FakeGuild()
        original = register_all(guild)[TRACKING_FEATURE]
        existing = asyncio.run(guild.create_role(name="Readers"))

        asyncio.run(pr.adopt_existing_role(guild, TRACKING_FEATURE, existing))

        self.assertEqual(pr.get_managed_role_id(str(guild.id), TRACKING_FEATURE), str(existing.id))
        self.assertEqual(existing.name, "Readers")
        self.assertEqual(original.name, pr.ROLE_NAMES[TRACKING_FEATURE])
        self.assertIn(original.id, guild._roles)

    def test_adopt_refuses_everyone_and_roles_already_managed(self):
        guild = FakeGuild()
        roles = register_all(guild)
        everyone = FakeRole("@everyone", guild.id)
        guild._roles[guild.id] = everyone

        with self.assertRaises(pr.ParticipationRoleError):
            asyncio.run(pr.adopt_existing_role(guild, TRACKING_FEATURE, everyone))
        with self.assertRaises(pr.ParticipationRoleError):
            asyncio.run(pr.adopt_existing_role(guild, TRACKING_FEATURE, roles[CLUB_FEATURE]))

    def test_adopt_refuses_role_above_the_bot(self):
        guild = FakeGuild()
        register_all(guild)
        high = FakeRole("Too High", 42, position=guild.me.top_role.position + 1)
        guild._roles[42] = high

        with self.assertRaises(pr.ParticipationRoleError):
            asyncio.run(pr.adopt_existing_role(guild, TRACKING_FEATURE, high))


class AdminRoleTests(ParticipationRolesTestBase):
    def test_last_admin_cannot_be_removed(self):
        guild = FakeGuild()
        roles = register_all(guild)
        alice = FakeMember(1, "Alice", guild)
        bob = FakeMember(2, "Bob", guild)
        guild.members.extend([alice, bob])

        self.assertEqual(asyncio.run(pr.set_admin_role(alice, True)), "granted")
        self.assertEqual(asyncio.run(pr.set_admin_role(alice, False)), "last_admin")
        self.assertIn(roles[pr.ADMIN_ROLE_KEY], alice.roles)

        self.assertEqual(asyncio.run(pr.set_admin_role(bob, True)), "granted")
        self.assertEqual(asyncio.run(pr.set_admin_role(alice, False)), "revoked")
        self.assertNotIn(roles[pr.ADMIN_ROLE_KEY], alice.roles)
        self.assertIn(roles[pr.ADMIN_ROLE_KEY], bob.roles)

    def test_authorization_uses_registered_admin_id_not_role_name(self):
        from helpers import has_admin_role

        guild = FakeGuild()
        roles = register_all(guild)
        roles[pr.ADMIN_ROLE_KEY].name = "Existing Moderators"
        managed_admin = FakeMember(3, "Cam", guild, roles=[roles[pr.ADMIN_ROLE_KEY]])
        colliding_name = FakeRole(pr.ROLE_NAMES[pr.ADMIN_ROLE_KEY], 777)
        squatter = FakeMember(4, "Dan", guild, roles=[colliding_name])

        self.assertTrue(has_admin_role(managed_admin))
        self.assertFalse(has_admin_role(squatter))


class HasAdminRoleOwnerFallbackTests(unittest.TestCase):
    def test_owner_counts_as_admin_even_without_the_role(self):
        from helpers import has_admin_role

        self.assertTrue(has_admin_role(_plain_member(owner=True, has_admin_role_name=False)))

    def test_regular_member_without_role_is_not_admin(self):
        from helpers import has_admin_role

        self.assertFalse(has_admin_role(_plain_member(owner=False, has_admin_role_name=False)))


if __name__ == "__main__":
    unittest.main()
