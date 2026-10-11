"""Small cross-layer helpers shared by the bot, handlers, views, and jobs.

Kept dependency-free apart from config/database so every layer can import
them without creating import cycles.
"""
import contextlib
import logging

import discord

from database import connect
from guild_settings import (
    CHANNEL_SETTING_KEYS,
    get_guild_settings,
    normalize_channel_name,
    update_guild_settings,
)
from poll_threads import get_poll_thread_by_discord_id


def has_admin_role(member) -> bool:
    """Return whether a member may administer the club in their current guild."""
    if not hasattr(member, "roles"):
        return False
    # Emergency path: the server owner always retains club administration.
    guild = getattr(member, "guild", None)
    if getattr(member, "id", None) == getattr(guild, "owner_id", None):
        return True
    # Fallback: anyone with Manage Server or Administrator permission
    permissions = getattr(member, "guild_permissions", None)
    if permissions and (permissions.manage_guild or permissions.administrator):
        return True
    guild_id = getattr(guild, "id", None)
    if guild_id is None:
        return False
    conn = connect()
    try:
        row = conn.execute(
            "SELECT role_id FROM managed_roles WHERE guild_id = ? AND role_key = 'admin'",
            (str(guild_id),),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return False
    return any(str(role.id) == str(row[0]) for role in member.roles)


def is_channel_snowflake(channel_ref: str) -> bool:
    """Return whether a stored channel setting looks like a Discord snowflake."""
    text = (channel_ref or "").strip()
    return bool(text) and text.isdigit()


def resolve_text_channel(guild, channel_ref: str):
    """Resolve a guild text channel from a snowflake ID or channel name.

    Empty refs return None. Digit strings prefer ID lookup; anything else is
    treated as a channel name (leading # stripped). Non-text channels are
    ignored so category/voice IDs cannot be used as club destinations.
    """
    if guild is None:
        return None
    text = (channel_ref or "").strip()
    if not text:
        return None
    if text.isdigit():
        channel = discord.utils.get(guild.text_channels, id=int(text))
        return channel
    name = normalize_channel_name(text)
    if not name:
        return None
    return discord.utils.get(guild.text_channels, name=name)


def get_channel_by_name(guild, channel_name: str):
    """Returns the named text channel, or None if not found.

    Prefer resolve_text_channel for settings values that may already be IDs.
    """
    return resolve_text_channel(guild, channel_name)


def channel_setting_label(channel_ref: str, guild=None, *, empty: str = "(not set)") -> str:
    """Human-facing label for a stored channel setting (mention, #name, or empty)."""
    text = (channel_ref or "").strip()
    if not text:
        return empty
    if text.isdigit():
        if guild is not None:
            channel = resolve_text_channel(guild, text)
            if channel is not None:
                return channel.mention
        return f"<#{text}>"
    return f"#{normalize_channel_name(text) or text}"


def channel_setting_edit_default(guild, channel_ref: str) -> str:
    """Modal default: show the live channel name when the setting is an ID."""
    text = (channel_ref or "").strip()
    if not text:
        return ""
    channel = resolve_text_channel(guild, text) if guild is not None else None
    if channel is not None:
        return channel.name
    return normalize_channel_name(text) if not text.isdigit() else text


def channel_matches_setting(channel, channel_ref: str) -> bool:
    """Return whether a Discord channel matches a stored name-or-ID setting."""
    text = (channel_ref or "").strip()
    if channel is None or not text:
        return False
    if text.isdigit():
        return str(channel.id) == text
    return getattr(channel, "name", None) == normalize_channel_name(text)


def resolve_channel_setting_to_id(guild, channel_ref: str) -> str | None:
    """Return a snowflake string when channel_ref resolves in guild, else None."""
    channel = resolve_text_channel(guild, channel_ref)
    if channel is None:
        return None
    return str(channel.id)


def upgrade_guild_channel_settings(guild) -> dict:
    """Rewrite name-valued channel settings to snowflake IDs when channels exist.

    Idempotent. Unresolved names are left alone so dual-read still works until
    the channel appears or an admin sets it explicitly.
    """
    if guild is None:
        raise ValueError("guild is required")
    guild_id = str(guild.id)
    settings = get_guild_settings(guild_id)
    changes = {}
    for key in CHANNEL_SETTING_KEYS:
        current = settings.get(key) or ""
        if not current or is_channel_snowflake(current):
            continue
        channel_id = resolve_channel_setting_to_id(guild, current)
        if channel_id and channel_id != current:
            changes[key] = channel_id
    if changes:
        return update_guild_settings(guild_id, **changes)
    return settings


def get_reply_channel(message):
    """Return the configured reply channel, or the message's channel as fallback.
    When a guild reply channel is set and that channel exists, all bot replies
    go there instead of the channel the user messaged from."""
    if message.guild:
        tracked = get_poll_thread_by_discord_id(str(message.guild.id), str(message.channel.id))
        if tracked and tracked["status"] == "open":
            return message.channel
        ref = get_guild_settings(str(message.guild.id))["reply_channel_name"]
        if ref:
            channel = resolve_text_channel(message.guild, ref)
            if channel:
                return channel
    return message.channel


@contextlib.asynccontextmanager
async def typing_indicator(channel):
    """Show the typing indicator while the block runs, if Discord allows it.

    The indicator is cosmetic, so a failure to start it (rate limit, missing
    permission) is logged and the block still runs.
    """
    stack = contextlib.AsyncExitStack()
    try:
        await stack.enter_async_context(channel.typing())
    except discord.HTTPException as exc:
        logging.getLogger("bookclub").warning("Typing indicator unavailable: %s", exc)
    async with stack:
        yield


def build_spoiler_thread_name(title: str, author: str | None = None) -> str:
    base = f"Spoilers: {title}"
    if author:
        base = f"{base} by {author}"
    return base[:100]


def find_user_by_name(name: str, guild_id: str) -> tuple[str | None, str | None]:
    """Returns (discord_id, display_name) for a user matching the given name, or (None, None)"""
    conn = connect()
    try:
        cur = conn.cursor()
        # Try exact match first
        cur.execute(
            "SELECT discord_id, display_name FROM users WHERE LOWER(display_name) = LOWER(?)",
            (name,)
        )
        row = cur.fetchone()
        if not row:
            # Fall back to partial match
            cur.execute(
                "SELECT discord_id, display_name FROM users WHERE LOWER(display_name) LIKE LOWER(?)",
                (f"%{name}%",)
            )
            row = cur.fetchone()
        if row:
            return row[0], row[1]
        return None, None
    finally:
        conn.close()


def _dashboard_date(value):
    """Keep operational timestamps compact without assuming a timezone format."""
    return str(value or "unknown")[:16].replace("T", " ")
