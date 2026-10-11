"""Persistent Discord threads for nomination and poll workflows."""

from datetime import datetime, timedelta, timezone

import discord

from database import connect
from guild_settings import get_guild_settings
from sending import role_broadcast_payload, send_embeds, send_summary


WORKFLOW_NOMINATIONS = "nominations"
WORKFLOW_BOOK_POLL = "book_poll"
WORKFLOW_TIEBREAKER = "tiebreaker"
WORKFLOW_COORDINATION = "coordination"


def _now():
    return datetime.now(timezone.utc)


def get_poll_thread(guild_id: str, workflow_type: str, workflow_id: int):
    conn = connect()
    try:
        row = conn.execute(
            """SELECT guild_id, workflow_type, workflow_id, parent_channel_id,
                      anchor_message_id, thread_id, status, closed_at,
                      delete_after, deleted_at
               FROM poll_threads
               WHERE guild_id = ? AND workflow_type = ? AND workflow_id = ?""",
            (str(guild_id), workflow_type, int(workflow_id)),
        ).fetchone()
        if not row:
            return None
        keys = (
            "guild_id", "workflow_type", "workflow_id", "parent_channel_id",
            "anchor_message_id", "thread_id", "status", "closed_at",
            "delete_after", "deleted_at",
        )
        return dict(zip(keys, row))
    finally:
        conn.close()


def get_open_poll_thread(guild_id: str, workflow_type: str):
    conn = connect()
    try:
        row = conn.execute(
            """SELECT guild_id, workflow_type, workflow_id, parent_channel_id,
                      anchor_message_id, thread_id, status, closed_at,
                      delete_after, deleted_at
               FROM poll_threads
               WHERE guild_id = ? AND workflow_type = ? AND status = 'open'
               ORDER BY workflow_id DESC LIMIT 1""",
            (str(guild_id), workflow_type),
        ).fetchone()
        if not row:
            return None
        keys = (
            "guild_id", "workflow_type", "workflow_id", "parent_channel_id",
            "anchor_message_id", "thread_id", "status", "closed_at",
            "delete_after", "deleted_at",
        )
        return dict(zip(keys, row))
    finally:
        conn.close()


def get_poll_thread_by_discord_id(guild_id: str, thread_id: str):
    conn = connect()
    try:
        row = conn.execute(
            "SELECT workflow_type, workflow_id, status FROM poll_threads WHERE guild_id = ? AND thread_id = ?",
            (str(guild_id), str(thread_id)),
        ).fetchone()
        return {"workflow_type": row[0], "workflow_id": row[1], "status": row[2]} if row else None
    finally:
        conn.close()


def list_pending_deletions(guild_id: str | None = None):
    conn = connect()
    try:
        sql = "SELECT guild_id, workflow_type, workflow_id, thread_id, delete_after FROM poll_threads WHERE status = 'closed' AND deleted_at IS NULL AND delete_after IS NOT NULL"
        params = ()
        if guild_id is not None:
            sql += " AND guild_id = ?"
            params = (str(guild_id),)
        return [
            dict(zip(("guild_id", "workflow_type", "workflow_id", "thread_id", "delete_after"), row))
            for row in conn.execute(sql, params).fetchall()
        ]
    finally:
        conn.close()


def record_poll_thread(guild_id, workflow_type, workflow_id, parent_channel_id, anchor_message_id, thread_id):
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO poll_threads
               (guild_id, workflow_type, workflow_id, parent_channel_id,
                anchor_message_id, thread_id)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(guild_id, workflow_type, workflow_id) DO UPDATE SET
                 parent_channel_id = excluded.parent_channel_id,
                 anchor_message_id = excluded.anchor_message_id,
                 thread_id = excluded.thread_id,
                 status = 'open', closed_at = NULL,
                 delete_after = NULL, deleted_at = NULL""",
            (str(guild_id), workflow_type, int(workflow_id), str(parent_channel_id),
             str(anchor_message_id), str(thread_id)),
        )
        conn.commit()
    finally:
        conn.close()


def close_poll_thread_record(guild_id, workflow_type, workflow_id):
    settings = get_guild_settings(str(guild_id))
    closed_at = _now()
    retention = int(settings["poll_thread_retention_days"])
    delete_after = closed_at + timedelta(days=retention) if retention else None
    conn = connect()
    try:
        conn.execute(
            """UPDATE poll_threads
               SET status = 'closed', closed_at = ?, delete_after = ?
               WHERE guild_id = ? AND workflow_type = ? AND workflow_id = ?""",
            (closed_at.isoformat(), delete_after.isoformat() if delete_after else None,
             str(guild_id), workflow_type, int(workflow_id)),
        )
        conn.commit()
    finally:
        conn.close()
    return delete_after


def mark_poll_thread_deleted(guild_id, workflow_type, workflow_id):
    conn = connect()
    try:
        conn.execute(
            """UPDATE poll_threads SET status = 'deleted', deleted_at = ?
               WHERE guild_id = ? AND workflow_type = ? AND workflow_id = ?""",
            (_now().isoformat(), str(guild_id), workflow_type, int(workflow_id)),
        )
        conn.commit()
    finally:
        conn.close()


async def resolve_poll_thread(guild, record):
    if not guild or not record:
        return None
    thread_id = int(record["thread_id"])
    getter = getattr(guild, "get_thread", None)
    thread = getter(thread_id) if callable(getter) else None
    if thread is not None:
        return thread
    fetcher = getattr(guild, "fetch_channel", None)
    if callable(fetcher):
        try:
            return await fetcher(thread_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
    return None


async def create_poll_thread(
    parent,
    guild_id: str,
    workflow_type: str,
    workflow_id: int,
    thread_name: str,
    anchor_title: str,
    anchor_description: str,
    embed,
    view,
):
    """Create the parent anchor and thread, with parent-channel fallback.

    Idempotent: if a poll thread record already exists for this workflow
    (e.g. two triggers raced past the window check), the anchor is not
    re-posted and None is returned so callers don't double-announce.
    """
    if get_poll_thread(guild_id, workflow_type, workflow_id) is not None:
        return get_poll_thread(guild_id, workflow_type, workflow_id)
    anchor_content, anchor_mentions = role_broadcast_payload(parent.guild, "club", ping=True)
    anchor = await send_summary(
        parent, anchor_title,
        f"{anchor_description}\nThe details and voting controls are in the thread below.",
        kind="poll", content=anchor_content, allowed_mentions=anchor_mentions,
    )
    try:
        try:
            thread = await anchor.create_thread(
                name=thread_name[:100],
                auto_archive_duration=get_guild_settings(str(guild_id))["thread_auto_archive_minutes"],
            )
        except TypeError:
            thread = await anchor.create_thread(name=thread_name[:100])
        record_poll_thread(
            guild_id, workflow_type, workflow_id, parent.id, anchor.id, thread.id,
        )
        await send_embeds(thread, [embed], view=view)
        return thread
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        await send_embeds(parent, [embed], view=view)
        return parent


async def close_and_archive_poll_thread(guild, guild_id: str, workflow_type: str, workflow_id: int):
    record = get_poll_thread(guild_id, workflow_type, workflow_id)
    if not record or record["status"] != "open":
        return None
    thread = await resolve_poll_thread(guild, record)
    if thread is not None:
        try:
            await thread.edit(locked=True, archived=True)
        except (discord.Forbidden, discord.HTTPException):
            pass
    return close_poll_thread_record(guild_id, workflow_type, workflow_id)


async def delete_poll_thread(guild, record):
    thread = await resolve_poll_thread(guild, record)
    if thread is not None:
        try:
            await thread.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
    mark_poll_thread_deleted(record["guild_id"], record["workflow_type"], record["workflow_id"])
