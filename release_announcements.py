"""Persistent changelog embed: one message per guild, edited in place."""
import asyncio

import config

from actions_social import release_announcements_enabled
from release_notes import (
    RELEASE_BOARD_TITLE,
    format_release_board,
    parse_release_announcements,
)


_SYNC_LOCKS: dict[str, asyncio.Lock] = {}
_SYNC_LOCKS_LOOP = None


def _sync_lock(guild_id: str) -> asyncio.Lock:
    global _SYNC_LOCKS, _SYNC_LOCKS_LOOP
    loop = asyncio.get_running_loop()
    if _SYNC_LOCKS_LOOP is not loop:
        _SYNC_LOCKS = {}
        _SYNC_LOCKS_LOOP = loop
    lock = _SYNC_LOCKS.get(guild_id)
    if lock is None:
        lock = asyncio.Lock()
        _SYNC_LOCKS[guild_id] = lock
    return lock


def _board_embed(announcements):
    from embeds import build_release_board_embed

    return build_release_board_embed(format_release_board(announcements))


def _get_release_channel(guild):
    """Import Discord helpers only when an announcement is actually synced."""
    from guild_settings import get_guild_settings
    from helpers import resolve_text_channel

    settings = get_guild_settings(str(guild.id))
    return resolve_text_channel(guild, settings["release_announcements_channel"])


def _log_delivery_failure(exc: Exception, guild_id: str) -> None:
    import errors

    errors.log_exception(
        exc,
        surface="release_announcement",
        guild_id=guild_id,
        action="announce_release",
    )


def _embed_matches(message, embed) -> bool:
    embeds = getattr(message, "embeds", None) or []
    if not embeds:
        return False
    current = embeds[0]
    return (
        getattr(current, "title", None) == embed.title
        and (getattr(current, "description", None) or "") == (embed.description or "")
    )


def _is_release_board(message) -> bool:
    author = getattr(message, "author", None)
    if author is not None and not getattr(author, "bot", True):
        return False
    embeds = getattr(message, "embeds", None) or []
    title = getattr(embeds[0], "title", None) if embeds else None
    return title == RELEASE_BOARD_TITLE


async def _fetch_stored_message(channel, message_id):
    import discord

    try:
        return await channel.fetch_message(int(message_id))
    except discord.NotFound:
        return None


async def _find_existing_board(channel):
    import discord

    try:
        history = channel.history(limit=100)
        async for message in history:
            if _is_release_board(message):
                return message
    except discord.NotFound:
        return None
    return None


async def _edit_board(message, embed) -> bool:
    import discord

    try:
        await message.edit(
            allowed_mentions=discord.AllowedMentions.none(),
            content=None,
            embed=embed,
        )
    except (discord.Forbidden, discord.HTTPException):
        return False
    return True


async def _post_board(channel, embed):
    import discord

    return await channel.send(
        embed=embed,
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def _apply_board(message, embed, guild_id: str, channel_id) -> bool:
    from database import set_release_panel_message_id

    if _embed_matches(message, embed):
        set_release_panel_message_id(guild_id, message.id, channel_id)
        return False
    if await _edit_board(message, embed):
        set_release_panel_message_id(guild_id, message.id, channel_id)
        return True
    return False


async def sync_release_board(guild, announcements=None) -> bool:
    """Create or refresh one guild's changelog embed. Never pings."""
    guild_id = str(guild.id)
    async with _sync_lock(guild_id):
        return await _sync_release_board(guild, announcements)


async def _sync_release_board(guild, announcements) -> bool:
    import discord
    from database import get_release_panel, set_release_panel_message_id
    from helpers import resolve_text_channel

    guild_id = str(guild.id)
    if not release_announcements_enabled(guild_id):
        return False
    if announcements is None:
        announcements = parse_release_announcements()
    if not announcements:
        return False

    embed = _board_embed(announcements)
    target = _get_release_channel(guild)
    panel = get_release_panel(guild_id)
    stored_channel = (
        resolve_text_channel(guild, panel["channel_id"])
        if panel and panel["channel_id"]
        else None
    )
    lookup_channel = stored_channel or target

    old_message = None
    if lookup_channel and panel and panel["message_id"]:
        try:
            old_message = await _fetch_stored_message(lookup_channel, panel["message_id"])
        except (discord.Forbidden, discord.HTTPException):
            # A permission or transient API failure does not prove the board is
            # gone. Keep stored ids and retry instead of posting a duplicate.
            return False

    if target is None:
        return False

    if old_message is not None and old_message.channel.id == target.id:
        return await _apply_board(old_message, embed, guild_id, target.id)

    found = None
    history_error = False
    try:
        found = await _find_existing_board(target)
    except (discord.Forbidden, discord.HTTPException):
        history_error = True

    if found is not None:
        if old_message is not None and old_message.id != found.id:
            try:
                await old_message.delete()
            except (discord.Forbidden, discord.HTTPException):
                pass
        return await _apply_board(found, embed, guild_id, target.id)

    if history_error and (old_message is not None or (panel and panel["message_id"])):
        return False

    if old_message:
        try:
            await old_message.delete()
        except (discord.Forbidden, discord.HTTPException):
            pass

    try:
        message = await _post_board(target, embed)
    except Exception as exc:
        _log_delivery_failure(exc, guild_id)
        return False
    set_release_panel_message_id(guild_id, message.id, target.id)
    return True


async def sync_release_boards(guilds, announcements=None) -> int:
    """Sync the changelog embed to each eligible guild once per ready/join."""
    if not config.RELEASE_ANNOUNCEMENTS_ENABLED:
        return 0
    if announcements is None:
        announcements = parse_release_announcements()
    if not announcements:
        return 0

    updated = 0
    for guild in guilds:
        try:
            if await sync_release_board(guild, announcements):
                updated += 1
        except Exception as exc:
            _log_delivery_failure(exc, str(getattr(guild, "id", "")))
    return updated
