"""Per-guild club settings.

config.py values are insert defaults for NEW guilds only. ensure_guild_settings
is idempotent (INSERT OR IGNORE) so later config.py edits never rewrite existing
rows. Changing settings for guilds that already have a row requires an explicit
data migration or an admin update.
"""
from __future__ import annotations

import config
from database import connect

BOOL_KEYS = (
    "member_opt_in_enabled",
    "weekly_recap_scheduled",
    "checkin_prompt_scheduled",
    "combined_weekly_post",
    "taste_profile_scheduled",
    "inactivity_nudge_scheduled",
    "listen_channel_only",
    "require_mention",
)

STR_KEYS = (
    "listen_channel_name",
    "reply_channel_name",
    "opt_in_welcome_channel",
    "nominations_channel",
    "weekly_recap_channel",
    "checkin_prompt_channel",
    "leaderboard_channel",
    "year_in_review_channel",
    "release_announcements_channel",
    "weekly_recap_day_of_week",
    "checkin_prompt_day_of_week",
)

INT_KEYS = (
    "weekly_recap_hour",
    "checkin_prompt_hour",
    "inactivity_nudge_days",
    "inactivity_nudge_interval_hours",
    "nominations_default_days",
    "poll_default_days",
    "coordination_poll_default_days",
    "thread_auto_archive_minutes",
    "poll_thread_retention_days",
)

SETTING_KEYS = BOOL_KEYS + STR_KEYS + INT_KEYS

REQUIRED_CHANNEL_KEYS = (
    "opt_in_welcome_channel",
    "nominations_channel",
    "weekly_recap_channel",
    "checkin_prompt_channel",
    "leaderboard_channel",
    "year_in_review_channel",
    "release_announcements_channel",
)

OPTIONAL_CHANNEL_KEYS = ("listen_channel_name", "reply_channel_name")

# Runtime values are Discord snowflake IDs when known; config.py defaults and
# not-yet-upgraded rows may still hold channel names. Dual-read both forms.
CHANNEL_SETTING_KEYS = REQUIRED_CHANNEL_KEYS + OPTIONAL_CHANNEL_KEYS

DAY_KEYS = ("weekly_recap_day_of_week", "checkin_prompt_day_of_week")
HOUR_KEYS = ("weekly_recap_hour", "checkin_prompt_hour")
POSITIVE_INT_KEYS = (
    "inactivity_nudge_days",
    "inactivity_nudge_interval_hours",
    "nominations_default_days",
    "poll_default_days",
    "coordination_poll_default_days",
)

VALID_DAYS = frozenset(("mon", "tue", "wed", "thu", "fri", "sat", "sun"))
VALID_THREAD_ARCHIVE_MINUTES = frozenset((60, 1440, 4320, 10080))

SCHEDULE_KEYS = frozenset((
    "weekly_recap_scheduled",
    "checkin_prompt_scheduled",
    "combined_weekly_post",
    "inactivity_nudge_scheduled",
    "inactivity_nudge_interval_hours",
    "weekly_recap_day_of_week",
    "weekly_recap_hour",
    "checkin_prompt_day_of_week",
    "checkin_prompt_hour",
))

_SELECT_SQL = "SELECT " + ", ".join(SETTING_KEYS) + " FROM guild_settings WHERE guild_id = ?"
_INSERT_SQL = (
    "INSERT INTO guild_settings (guild_id, " + ", ".join(SETTING_KEYS) + ") "
    "VALUES (?, " + ", ".join("?" for _ in SETTING_KEYS) + ") "
    "ON CONFLICT(guild_id) DO NOTHING"
)


class SettingsError(ValueError):
    """Invalid guild-settings update."""


def default_settings() -> dict:
    """Current config.py club defaults, used only when inserting a new guild row."""
    return {
        "member_opt_in_enabled": bool(config.MEMBER_OPT_IN_ENABLED),
        "weekly_recap_scheduled": bool(config.WEEKLY_RECAP_SCHEDULED),
        "checkin_prompt_scheduled": bool(config.CHECKIN_PROMPT_SCHEDULED),
        "combined_weekly_post": bool(config.COMBINED_WEEKLY_POST),
        "taste_profile_scheduled": bool(config.TASTE_PROFILE_SCHEDULED),
        "inactivity_nudge_scheduled": bool(config.INACTIVITY_NUDGE_SCHEDULED),
        "listen_channel_only": bool(config.LISTEN_CHANNEL_ONLY),
        "require_mention": bool(config.REQUIRE_MENTION),
        "listen_channel_name": config.LISTEN_CHANNEL_NAME or "",
        "reply_channel_name": config.REPLY_CHANNEL_NAME or "",
        "opt_in_welcome_channel": config.OPT_IN_WELCOME_CHANNEL or "",
        "nominations_channel": config.NOMINATIONS_CHANNEL or "",
        "weekly_recap_channel": config.WEEKLY_RECAP_CHANNEL or "",
        "checkin_prompt_channel": config.CHECKIN_PROMPT_CHANNEL or "",
        "leaderboard_channel": config.LEADERBOARD_CHANNEL or "",
        "year_in_review_channel": config.YEAR_IN_REVIEW_CHANNEL or "",
        "release_announcements_channel": config.RELEASE_ANNOUNCEMENTS_CHANNEL or "",
        "weekly_recap_day_of_week": config.WEEKLY_RECAP_DAY_OF_WEEK,
        "checkin_prompt_day_of_week": config.CHECKIN_PROMPT_DAY_OF_WEEK,
        "weekly_recap_hour": int(config.WEEKLY_RECAP_HOUR),
        "checkin_prompt_hour": int(config.CHECKIN_PROMPT_HOUR),
        "inactivity_nudge_days": int(config.INACTIVITY_NUDGE_DAYS),
        "inactivity_nudge_interval_hours": int(config.INACTIVITY_NUDGE_INTERVAL_HOURS),
        "nominations_default_days": int(config.NOMINATIONS_DEFAULT_DAYS),
        "poll_default_days": int(config.POLL_DEFAULT_DAYS),
        "coordination_poll_default_days": int(config.COORDINATION_POLL_DEFAULT_DAYS),
        "thread_auto_archive_minutes": int(config.THREAD_AUTO_ARCHIVE_MINUTES),
        "poll_thread_retention_days": int(config.POLL_THREAD_RETENTION_DAYS),
    }


def _row_to_settings(row) -> dict:
    values = {}
    for index, key in enumerate(SETTING_KEYS):
        value = row[index]
        if key in BOOL_KEYS:
            values[key] = bool(value)
        elif key in INT_KEYS:
            values[key] = int(value)
        else:
            values[key] = value or ""
    return values


def _insert_params(settings: dict):
    params = []
    for key in SETTING_KEYS:
        value = settings[key]
        if key in BOOL_KEYS:
            params.append(int(bool(value)))
        else:
            params.append(value)
    return params


def _require_guild_id(guild_id: str) -> str:
    if not guild_id:
        raise ValueError("guild_id is required")
    return str(guild_id)


def ensure_guild_settings(guild_id: str) -> dict:
    """Create a settings row from current config defaults if this guild has none.

    Idempotent: an existing row is left untouched, including when config.py
    defaults have changed since the row was inserted.
    """
    guild_id = _require_guild_id(guild_id)
    conn = connect()
    try:
        row = conn.execute(_SELECT_SQL, (guild_id,)).fetchone()
        if row is None:
            conn.execute(_INSERT_SQL, (guild_id, *_insert_params(default_settings())))
            conn.commit()
            row = conn.execute(_SELECT_SQL, (guild_id,)).fetchone()
        return _row_to_settings(row)
    finally:
        conn.close()


def get_guild_settings(guild_id: str) -> dict:
    """Return this guild's settings, creating the row if needed."""
    return ensure_guild_settings(guild_id)


def normalize_channel_name(value: str) -> str:
    text = (value or "").strip()
    if text.startswith("#"):
        text = text[1:].strip()
    return text


def normalize_channel_setting(value: str) -> str:
    """Normalize a channel setting to a snowflake string or channel name.

    Digit-only values are kept as Discord channel IDs. Other values are treated
    as channel names (leading # stripped). Empty string clears optional channels.
    """
    text = (value or "").strip()
    if not text:
        return ""
    if text.isdigit():
        return text
    return normalize_channel_name(text)


def _coerce_update(key: str, value):
    if key not in SETTING_KEYS:
        raise SettingsError(f"Unknown setting: {key}")
    if key in BOOL_KEYS:
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in ("1", "true", "yes", "on"):
                return True
            if lowered in ("0", "false", "no", "off"):
                return False
            raise SettingsError(f"{key} must be on or off.")
        return bool(value)
    if key in DAY_KEYS:
        day = str(value).strip().lower()[:3]
        if day not in VALID_DAYS:
            raise SettingsError("Day must be mon, tue, wed, thu, fri, sat, or sun.")
        return day
    if key in HOUR_KEYS:
        try:
            hour = int(value)
        except (TypeError, ValueError):
            raise SettingsError("Hour must be a whole number from 0 to 23.") from None
        if hour < 0 or hour > 23:
            raise SettingsError("Hour must be a whole number from 0 to 23.")
        return hour
    if key == "thread_auto_archive_minutes":
        try:
            minutes = int(value)
        except (TypeError, ValueError):
            raise SettingsError("Thread archive must be 60, 1440, 4320, or 10080 minutes.") from None
        if minutes not in VALID_THREAD_ARCHIVE_MINUTES:
            raise SettingsError("Thread archive must be 60, 1440, 4320, or 10080 minutes.")
        return minutes
    if key == "poll_thread_retention_days":
        try:
            days = int(value)
        except (TypeError, ValueError):
            raise SettingsError("Poll thread retention must be 0–365 days.") from None
        if days < 0 or days > 365:
            raise SettingsError("Poll thread retention must be 0–365 days.")
        return days
    if key in POSITIVE_INT_KEYS:
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise SettingsError(f"{key} must be a positive whole number.") from None
        if key.endswith("_days") and (number < 1 or number > 365):
            raise SettingsError("Duration must be between 1 and 365 days.")
        if number < 1:
            raise SettingsError(f"{key} must be a positive whole number.")
        return number
    if key in CHANNEL_SETTING_KEYS:
        setting = normalize_channel_setting(str(value))
        if key in REQUIRED_CHANNEL_KEYS and not setting:
            raise SettingsError("Channel cannot be empty.")
        return setting
    return value


def update_guild_settings(guild_id: str, **changes) -> dict:
    """Update selected columns for an existing guild. Unknown keys are rejected."""
    guild_id = _require_guild_id(guild_id)
    if not changes:
        return get_guild_settings(guild_id)
    coerced = {key: _coerce_update(key, value) for key, value in changes.items()}
    current = ensure_guild_settings(guild_id)
    proposed = {**current, **coerced}
    if proposed["listen_channel_only"] and not proposed["listen_channel_name"]:
        raise SettingsError(
            "Set a listen channel before enabling listen-channel only."
        )
    assignments = ", ".join(f"{key} = ?" for key in coerced)
    values = [
        int(bool(value)) if key in BOOL_KEYS else value
        for key, value in coerced.items()
    ]
    conn = connect()
    try:
        conn.execute(
            f"UPDATE guild_settings SET {assignments}, updated_at = CURRENT_TIMESTAMP WHERE guild_id = ?",
            (*values, guild_id),
        )
        conn.commit()
    finally:
        conn.close()
    return get_guild_settings(guild_id)
