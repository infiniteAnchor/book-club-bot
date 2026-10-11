"""Managed Discord participation roles.

Owns creation, lookup, assignment, bootstrap, and reconciliation of the bot's
managed roles. Discord roles are the live participation state; SQLite mirrors
them per guild through actions_social. Registry safety rules:

- The bot never deletes or renames an existing role.
- A stored managed role is located by its stored Discord ID only; the bot never
  adopts a role merely because its name matches.
- On a name collision at creation time, a suffixed name is used instead.
- If a stored managed role was deleted on Discord, a replacement is created,
  the registry is updated, and membership is resynchronized by callers.
"""
import discord

from config import NOMINATIONS_ADMIN_ROLE
from database import connect
from actions_social import (
    CLUB_FEATURE,
    DM_NUDGES_FEATURE,
    TRACKING_FEATURE,
    UPDATES_FEATURE,
    get_member_feature_preferences,
    normalized_participation_preferences,
    set_member_feature_preferences,
)

CHAT_ROLE_KEY = "chat"
ADMIN_ROLE_KEY = "admin"

# Tier keys double as feature keys in member_feature_preferences.
TIER_ROLE_KEYS = (TRACKING_FEATURE, UPDATES_FEATURE, CLUB_FEATURE)
NOTIFICATION_ROLE_KEYS = TIER_ROLE_KEYS + (DM_NUDGES_FEATURE,)
MANAGED_ROLE_KEYS = (CHAT_ROLE_KEY,) + NOTIFICATION_ROLE_KEYS + (ADMIN_ROLE_KEY,)

ROLE_NAMES = {
    CHAT_ROLE_KEY: "Book Club · Chat",
    TRACKING_FEATURE: "Book Club · Tracking",
    UPDATES_FEATURE: "Book Club · Updates",
    CLUB_FEATURE: "Book Club · Club",
    DM_NUDGES_FEATURE: "Book Club · DM Nudges",
    ADMIN_ROLE_KEY: NOMINATIONS_ADMIN_ROLE,
}

ROLE_COLOURS = {
    CHAT_ROLE_KEY: discord.Colour.greyple(),
    TRACKING_FEATURE: discord.Colour.green(),
    UPDATES_FEATURE: discord.Colour.gold(),
    CLUB_FEATURE: discord.Colour.blurple(),
    DM_NUDGES_FEATURE: discord.Colour.purple(),
    ADMIN_ROLE_KEY: discord.Colour.red(),
}

_COLLISION_SUFFIX = "(Bot)"

ASSIGN_REASON = "Book Club participation update"


class ParticipationRoleError(RuntimeError):
    """Raised when the bot cannot safely manage its participation roles."""


# ── Managed-role registry ────────────────────────────────────────────────────

def get_managed_role_id(guild_id: str, role_key: str) -> str | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT role_id FROM managed_roles WHERE guild_id = ? AND role_key = ?",
            (guild_id, role_key),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def get_managed_role_ids(guild_id: str) -> dict[str, str]:
    """Return every stored managed-role ID for a guild as key -> role_id."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT role_key, role_id FROM managed_roles WHERE guild_id = ?",
            (guild_id,),
        ).fetchall()
        return {role_key: role_id for role_key, role_id in rows}
    finally:
        conn.close()


def set_managed_role_id(guild_id: str, role_key: str, role_id: int | str) -> None:
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO managed_roles (guild_id, role_key, role_id)
               VALUES (?, ?, ?)
               ON CONFLICT(guild_id, role_key)
               DO UPDATE SET role_id = excluded.role_id, updated_at = CURRENT_TIMESTAMP""",
            (guild_id, role_key, str(role_id)),
        )
        conn.commit()
    finally:
        conn.close()


def get_sync_completed_at(guild_id: str) -> str | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT completed_at FROM role_sync_state WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def mark_sync_complete(guild_id: str) -> None:
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO role_sync_state (guild_id, completed_at)
               VALUES (?, CURRENT_TIMESTAMP)
               ON CONFLICT(guild_id) DO UPDATE SET completed_at = CURRENT_TIMESTAMP""",
            (guild_id,),
        )
        conn.commit()
    finally:
        conn.close()


# ── Role creation and lookup ─────────────────────────────────────────────────

def resolve(guild, role_key: str):
    """Return the stored managed role for a guild, or None. Stored ID only."""
    role_id = get_managed_role_id(str(guild.id), role_key)
    if not role_id:
        return None
    return guild.get_role(int(role_id))


async def ensure_managed_roles(guild) -> dict[str, discord.Role]:
    """Create missing managed roles and register their IDs.

    Returns role_key -> Role. Raises ParticipationRoleError when the bot lacks
    Manage Roles or sits below an existing managed role.
    """
    me = guild.me
    if me is None or not me.guild_permissions.manage_roles:
        raise ParticipationRoleError(
            f"Bot lacks Manage Roles in guild {guild.id}; cannot manage participation roles."
        )
    roles = {}
    for role_key in MANAGED_ROLE_KEYS:
        role = await _locate_or_create(guild, role_key)
        if me.top_role is None or role.position >= me.top_role.position:
            raise ParticipationRoleError(
                f"Managed role {role.name!r} is not below the bot's top role in guild {guild.id}."
            )
        roles[role_key] = role
    return roles


async def _locate_or_create(guild, role_key: str) -> discord.Role:
    role_id = get_managed_role_id(str(guild.id), role_key)
    if role_id:
        role = guild.get_role(int(role_id))
        if role is not None:
            return role
        # Stored role was deleted on Discord: recreate and re-register.
        role = await _create_managed_role(guild, role_key)
        set_managed_role_id(str(guild.id), role_key, role.id)
        return role

    role = await _create_managed_role(guild, role_key)
    set_managed_role_id(str(guild.id), role_key, role.id)
    return role


async def _create_managed_role(guild, role_key: str) -> discord.Role:
    base_name = ROLE_NAMES[role_key]
    taken = {role.name for role in guild.roles}
    # Existing roles are never adopted or altered; collisions get a suffix.
    name = base_name
    attempt = 0
    while name in taken:
        attempt += 1
        name = f"{base_name} {_COLLISION_SUFFIX}" if attempt == 1 else f"{base_name} {_COLLISION_SUFFIX} {attempt}"
    return await guild.create_role(
        name=name,
        colour=ROLE_COLOURS[role_key],
        reason="Book Club bot managed-role setup",
    )


# ── Assignment ───────────────────────────────────────────────────────────────

async def apply_tier_roles(member, desired: dict) -> None:
    """Make the member's managed notification/tier roles match `desired`.

    Chat is ensured but never removed; only keys present in `desired` are
    considered for changes. Missing managed roles are skipped silently —
    bootstrap/reconcile own creating them.
    """
    to_add, to_remove = [], []
    for feature_key in NOTIFICATION_ROLE_KEYS:
        if feature_key not in desired:
            continue
        role = resolve(member.guild, feature_key)
        if role is None:
            continue
        has = role in member.roles
        if desired[feature_key] and not has:
            to_add.append(role)
        elif not desired[feature_key] and has:
            to_remove.append(role)
    chat_role = resolve(member.guild, CHAT_ROLE_KEY)
    if chat_role is not None and chat_role not in member.roles:
        to_add.append(chat_role)
    if to_add:
        await member.add_roles(*to_add, reason=ASSIGN_REASON)
    if to_remove:
        await member.remove_roles(*to_remove, reason=ASSIGN_REASON)


async def set_participation(member, *, tracking=None, updates=None, club=None, dm_nudges=None) -> dict:
    """Apply participation roles, then persist the successfully applied state."""
    guild_id = str(member.guild.id)
    prefs = normalized_participation_preferences(
        get_member_feature_preferences(str(member.id), guild_id),
        tracking=tracking, updates=updates, club=club, dm_nudges=dm_nudges,
    )
    await ensure_managed_roles(member.guild)
    try:
        await apply_tier_roles(member, prefs)
    except (discord.Forbidden, discord.HTTPException) as exc:
        raise ParticipationRoleError(
            f"Discord rejected the participation-role update in guild {guild_id}."
        ) from exc
    return set_member_feature_preferences(
        str(member.id), str(member.display_name), guild_id,
        **prefs,
    )


def tier_state_from_roles(guild, member) -> dict:
    """Read the member's current tier state from their managed Discord roles."""
    state = {}
    for feature_key in NOTIFICATION_ROLE_KEYS:
        role = resolve(guild, feature_key)
        state[feature_key] = bool(role) and role in member.roles
    return state


def normalized_tier_state(state: dict) -> tuple[dict, dict]:
    """Repair cumulative-invariant violations without removing higher tiers.

    Returns (repaired_state, deltas) where deltas holds only the keys that
    changed. Missing prerequisites are granted; an orphaned DM Nudges grant
    (no Tracking) is stripped. Nothing else is ever removed here.
    """
    repaired = dict(state)
    if repaired.get(CLUB_FEATURE):
        repaired[UPDATES_FEATURE] = True
    if repaired.get(UPDATES_FEATURE) or repaired.get(CLUB_FEATURE):
        repaired[TRACKING_FEATURE] = True
    if repaired.get(DM_NUDGES_FEATURE) and not repaired.get(TRACKING_FEATURE):
        repaired[DM_NUDGES_FEATURE] = False
    deltas = {key: value for key, value in repaired.items() if state.get(key) != value}
    return repaired, deltas


async def sync_member_from_discord(member, *, restore_keys=()) -> dict:
    """Mirror managed roles into SQLite, restoring recreated roles from its mirror."""
    state = tier_state_from_roles(member.guild, member)
    desired_state = dict(state)
    if restore_keys:
        mirrored = get_member_feature_preferences(str(member.id), str(member.guild.id))
        for feature_key in restore_keys:
            if feature_key in NOTIFICATION_ROLE_KEYS:
                desired_state[feature_key] = mirrored[feature_key]
    repaired, _ = normalized_tier_state(desired_state)
    deltas = {
        key: value
        for key, value in repaired.items()
        if state.get(key) != value
    }
    if deltas:
        await apply_tier_roles(member, repaired)
    prefs = set_member_feature_preferences(
        str(member.id), str(member.display_name), str(member.guild.id),
        **{feature_key: repaired[feature_key] for feature_key in NOTIFICATION_ROLE_KEYS},
    )
    return {"state": state, "deltas": deltas, "prefs": prefs}


def managed_keys_changed(before, after) -> bool:
    """True when the change touched one of this guild's managed roles."""
    before_map = get_managed_role_ids(str(before.guild.id))
    after_map = get_managed_role_ids(str(after.guild.id))
    before_keys = {key for key, role_id in before_map.items() if int(role_id) in {r.id for r in before.roles}}
    after_keys = {key for key, role_id in after_map.items() if int(role_id) in {r.id for r in after.roles}}
    return before_keys != after_keys


async def handle_member_update(before, after) -> dict | None:
    """on_member_update listener: immediately mirror manual managed-role changes.

    Ignores unrelated role changes entirely. Bot-originated updates land on an
    already-normalized state, so the second event they trigger is a no-op.
    """
    if after.bot:
        return None
    if not managed_keys_changed(before, after):
        return None
    return await sync_member_from_discord(after)


# ── Bootstrap and reconciliation ─────────────────────────────────────────────

async def restore_on_rejoin(member) -> None:
    """Give a joining/rejoining human their Chat role plus persisted tiers."""
    if member.bot:
        return
    prefs = get_member_feature_preferences(str(member.id), str(member.guild.id))
    await apply_tier_roles(member, prefs)


async def run_startup_sync(guild) -> str:
    """Bootstrap once per guild, then reconcile on every startup afterwards."""
    if get_sync_completed_at(str(guild.id)) is None:
        await bootstrap_guild(guild)
        return "bootstrap"
    await reconcile_guild(guild)
    return "reconcile"


async def bootstrap_guild(guild) -> int:
    """One-time pass from migrated DB preferences to Discord roles.

    Every non-bot human gets Chat; persisted Tracking/Updates/Club/DM Nudge
    preferences become roles. The completion marker is written only when every
    member was processed; failures are raised so the next startup retries.
    """
    roles = await ensure_managed_roles(guild)
    await _grant_admin_to_owner(guild, roles[ADMIN_ROLE_KEY])

    processed = 0
    failed = []
    if not guild.chunked:
        await guild.chunk()
    for member in guild.members:
        if member.bot:
            continue
        try:
            prefs = get_member_feature_preferences(str(member.id), str(guild.id))
            await apply_tier_roles(member, prefs)
            processed += 1
        except Exception:
            failed.append(str(member.id))
    if failed:
        raise ParticipationRoleError(
            f"Role bootstrap incomplete in guild {guild.id}; retrying members later: {failed}"
        )

    mark_sync_complete(str(guild.id))
    return processed


async def reconcile_guild(guild) -> dict:
    """Discord -> DB reconciliation: restore Chat, fix invariant violations."""
    registered = get_managed_role_ids(str(guild.id))
    recreated_keys = {
        role_key
        for role_key in MANAGED_ROLE_KEYS
        if role_key not in registered or guild.get_role(int(registered[role_key])) is None
    }
    roles = await ensure_managed_roles(guild)
    if ADMIN_ROLE_KEY in recreated_keys:
        await _grant_admin_to_owner(guild, roles[ADMIN_ROLE_KEY])

    restored_chat = 0
    repaired_members = 0
    if not guild.chunked:
        await guild.chunk()
    for member in guild.members:
        if member.bot:
            continue
        chat_role = resolve(guild, CHAT_ROLE_KEY)
        missing_chat = chat_role is not None and chat_role not in member.roles
        result = await sync_member_from_discord(
            member,
            restore_keys=recreated_keys.intersection(NOTIFICATION_ROLE_KEYS),
        )
        if missing_chat:
            restored_chat += 1
        if result["deltas"]:
            repaired_members += 1
    return {
        "restored_chat": restored_chat,
        "repaired": repaired_members,
        "recreated": sorted(recreated_keys),
    }


async def _grant_admin_to_owner(guild, admin_role) -> None:
    owner = guild.owner or guild.get_member(guild.owner_id)
    if owner is None or owner.bot:
        return
    if admin_role in owner.roles:
        return
    await owner.add_roles(admin_role, reason="Book Club admin bootstrap")


def admin_role_holders(guild) -> list:
    """Human members currently holding the managed Book Club Admin role."""
    role = resolve(guild, ADMIN_ROLE_KEY)
    if role is None:
        return []
    return [member for member in guild.members if not getattr(member, "bot", False) and role in member.roles]


def is_last_admin(guild, member) -> bool:
    """True when `member` is the only human holding the managed admin role."""
    holders = admin_role_holders(guild)
    return len(holders) == 1 and holders[0].id == member.id


async def set_admin_role(member, enabled: bool) -> str:
    """Grant or revoke Book Club Admin. Last-admin protection blocks revocation.

    Returns one of: granted, revoked, unchanged, last_admin, missing_role.
    """
    role = resolve(member.guild, ADMIN_ROLE_KEY)
    if role is None:
        return "missing_role"
    has_role = role in member.roles
    if enabled:
        if has_role:
            return "unchanged"
        await member.add_roles(role, reason=ASSIGN_REASON)
        return "granted"
    if not has_role:
        return "unchanged"
    if is_last_admin(member.guild, member):
        return "last_admin"
    await member.remove_roles(role, reason=ASSIGN_REASON)
    return "revoked"


async def adopt_existing_role(guild, role_key: str, role) -> None:
    """Point a managed-role key at an existing Discord role. Never auto-adopts.

    The previous registered role is left untouched (not deleted, renamed, or
    stripped). Callers should reconcile membership after a successful adopt.
    """
    if role_key not in MANAGED_ROLE_KEYS:
        raise ParticipationRoleError(f"Unknown managed role key {role_key!r}.")
    if role is None:
        raise ParticipationRoleError("No Discord role was selected to adopt.")
    is_default = getattr(role, "is_default", False)
    if callable(is_default):
        is_default = is_default()
    if role.id == guild.id or is_default:
        raise ParticipationRoleError("The @everyone role cannot be adopted.")
    me = guild.me
    if me is None or not me.guild_permissions.manage_roles:
        raise ParticipationRoleError(
            f"Bot lacks Manage Roles in guild {guild.id}; cannot adopt a role."
        )
    if me.top_role is None or role.position >= me.top_role.position:
        raise ParticipationRoleError(
            f"Adopted role {role.name!r} is not below the bot's top role in guild {guild.id}."
        )
    for key, stored_id in get_managed_role_ids(str(guild.id)).items():
        if str(stored_id) == str(role.id) and key != role_key:
            raise ParticipationRoleError(
                f"{role.name!r} is already registered as {ROLE_NAMES[key]}."
            )
    set_managed_role_id(str(guild.id), role_key, role.id)
