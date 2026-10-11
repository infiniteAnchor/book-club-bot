"""Participation preferences, tiers, and release-announcement settings."""
from datetime import datetime, timezone

from database import connect
from actions import get_or_create_user
import config


TRACKING_FEATURE = "tracking"
UPDATES_FEATURE = "updates"
CLUB_FEATURE = "club"
DM_NUDGES_FEATURE = "dm_nudges"
PARTICIPATION_FEATURES = (TRACKING_FEATURE, UPDATES_FEATURE, CLUB_FEATURE, DM_NUDGES_FEATURE)
TIER_PARTICIPATION_ARGS = {
    "chat": {"tracking": False, "dm_nudges": False},
    # Selecting a tier is an exact tier choice, not merely an upgrade. Keep
    # nudges independent so a tier change never silently changes that setting.
    "tracking": {"tracking": True, "updates": False, "club": False},
    "updates": {"tracking": True, "updates": True, "club": False},
    "club": {"tracking": True, "updates": True, "club": True},
}


def describe_participation(preferences: dict) -> str:
    """One-line status used by chat, enrollment, and the admin dashboard."""
    if preferences.get(CLUB_FEATURE):
        tier_name = "Club"
    elif preferences.get(UPDATES_FEATURE):
        tier_name = "Updates"
    elif preferences.get(TRACKING_FEATURE):
        tier_name = "Tracking"
    else:
        tier_name = "Chat"
    nudge_status = "on" if preferences.get(DM_NUDGES_FEATURE) else "off"
    return f"Your book club tier is **{tier_name}**. Reading nudge DMs are **{nudge_status}**."


def get_guild_release_announcement_preference(guild_id: str) -> bool | None:
    """Return a guild's explicit setting, or None when it inherits the default."""
    if not guild_id:
        return None
    conn = connect()
    try:
        row = conn.execute(
            """SELECT enabled FROM guild_release_announcement_settings
               WHERE guild_id = ?""",
            (guild_id,),
        ).fetchone()
        return bool(row[0]) if row else None
    finally:
        conn.close()


def release_announcements_enabled(guild_id: str) -> bool:
    """Return whether release announcements may be sent to this guild."""
    if not config.RELEASE_ANNOUNCEMENTS_ENABLED:
        return False
    preference = get_guild_release_announcement_preference(guild_id)
    return preference is not False


def set_guild_release_announcements_enabled(guild_id: str, enabled: bool) -> bool:
    """Persist a guild administrator's release-announcement preference."""
    if not guild_id:
        return False
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO guild_release_announcement_settings (guild_id, enabled)
               VALUES (?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET
                 enabled = excluded.enabled, updated_at = CURRENT_TIMESTAMP""",
            (guild_id, int(enabled)),
        )
        conn.commit()
        return bool(enabled)
    finally:
        conn.close()


def release_announcement_was_sent(guild_id: str, version: str) -> bool:
    """Return whether this guild has already received this release version."""
    conn = connect()
    try:
        row = conn.execute(
            """SELECT 1 FROM guild_release_announcements
               WHERE guild_id = ? AND version = ?""",
            (guild_id, version),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def record_release_announcement_sent(guild_id: str, version: str) -> bool:
    """Record a successful delivery. Returns false when it was already recorded."""
    conn = connect()
    try:
        cur = conn.execute(
            """INSERT INTO guild_release_announcements (guild_id, version)
               VALUES (?, ?) ON CONFLICT(guild_id, version) DO NOTHING""",
            (guild_id, version),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def get_member_feature_preferences(discord_id: str, guild_id: str) -> dict:
    """Return member participation preferences; absent rows are disabled."""
    preferences = {feature: False for feature in PARTICIPATION_FEATURES}
    if not guild_id:
        return preferences
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users WHERE discord_id = ?", (discord_id,))
        row = cur.fetchone()
        if not row:
            return preferences
        cur.execute(
            """SELECT feature_key, enabled FROM member_feature_preferences
               WHERE guild_id = ? AND user_id = ? AND feature_key IN (?, ?, ?, ?)""",
            (guild_id, row[0], *PARTICIPATION_FEATURES)
        )
        for feature_key, enabled in cur.fetchall():
            preferences[feature_key] = bool(enabled)
        return preferences
    finally:
        conn.close()


def has_tracking(discord_id: str, guild_id: str) -> bool:
    """Return whether the member has the Tracking tier in this guild."""
    if not guild_id:
        return False
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT 1
               FROM member_feature_preferences p
               JOIN users u ON u.user_id = p.user_id
               WHERE u.discord_id = ? AND p.guild_id = ?
                 AND p.feature_key = ? AND p.enabled = 1""",
            (discord_id, guild_id, TRACKING_FEATURE)
        )
        return cur.fetchone() is not None
    finally:
        conn.close()


def has_club(discord_id: str, guild_id: str) -> bool:
    """Return whether the member has the Club tier in this guild."""
    if not guild_id:
        return False
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT 1
               FROM member_feature_preferences p
               JOIN users u ON u.user_id = p.user_id
               WHERE u.discord_id = ? AND p.guild_id = ?
                 AND p.feature_key = ? AND p.enabled = 1""",
            (discord_id, guild_id, CLUB_FEATURE)
        )
        return cur.fetchone() is not None
    finally:
        conn.close()


def normalized_participation_preferences(current: dict, *, tracking=None, updates=None,
                                         club=None, dm_nudges=None) -> dict:
    """Return a normalized participation state without writing it to SQLite."""
    normalized = {
        feature: bool(current.get(feature, False))
        for feature in PARTICIPATION_FEATURES
    }
    requested = {
        TRACKING_FEATURE: tracking,
        UPDATES_FEATURE: updates,
        CLUB_FEATURE: club,
        DM_NUDGES_FEATURE: dm_nudges,
    }
    for feature, value in requested.items():
        if value is not None:
            normalized[feature] = bool(value)

    # An explicit Tracking opt-out takes precedence over every requirement
    # below and cascades to everything above it.
    if tracking is False:
        normalized[UPDATES_FEATURE] = False
        normalized[CLUB_FEATURE] = False
        normalized[DM_NUDGES_FEATURE] = False

    # Granting a tier grants its prerequisites; DM Nudges requires Tracking.
    if normalized[CLUB_FEATURE]:
        normalized[UPDATES_FEATURE] = True
    if normalized[UPDATES_FEATURE] or normalized[DM_NUDGES_FEATURE]:
        normalized[TRACKING_FEATURE] = True

    # Removing Tracking removes everything above; removing Updates removes Club.
    if not normalized[TRACKING_FEATURE]:
        normalized[UPDATES_FEATURE] = False
        normalized[CLUB_FEATURE] = False
        normalized[DM_NUDGES_FEATURE] = False
    elif not normalized[UPDATES_FEATURE]:
        normalized[CLUB_FEATURE] = False
    return normalized


def set_member_feature_preferences(discord_id: str, display_name: str, guild_id: str,
                                   *, tracking=None, updates=None, club=None,
                                   dm_nudges=None) -> dict:
    """Atomically update the participation mirror while enforcing tier invariants.

    Tiers are cumulative: Club implies Updates implies Tracking. Removing
    Tracking cascades upward; removing Club preserves Updates and Tracking.
    DM Nudges requires Tracking but never changes Updates or Club.
    """
    if not guild_id:
        return {feature: False for feature in PARTICIPATION_FEATURES}
    conn = connect()
    try:
        user_id = get_or_create_user(conn, discord_id, display_name)
        current = normalized_participation_preferences(
            get_member_feature_preferences(discord_id, guild_id),
            tracking=tracking,
            updates=updates,
            club=club,
            dm_nudges=dm_nudges,
        )

        now = datetime.now(timezone.utc).isoformat()
        cur = conn.cursor()
        for feature_key, enabled in current.items():
            cur.execute(
                """INSERT INTO member_feature_preferences
                   (user_id, guild_id, feature_key, enabled, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(guild_id, user_id, feature_key)
                   DO UPDATE SET enabled = excluded.enabled, updated_at = excluded.updated_at""",
                (user_id, guild_id, feature_key, int(enabled), now)
            )
        conn.commit()
        return current
    finally:
        conn.close()


def get_single_participation_guild(discord_id: str):
    """Return the only guild where a member participates (Tracking), else None."""
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT p.guild_id
               FROM member_feature_preferences p
               JOIN users u ON u.user_id = p.user_id
               WHERE u.discord_id = ? AND p.feature_key = ? AND p.enabled = 1""",
            (discord_id, TRACKING_FEATURE)
        )
        guild_ids = [row[0] for row in cur.fetchall()]
        return guild_ids[0] if len(guild_ids) == 1 else None
    finally:
        conn.close()
