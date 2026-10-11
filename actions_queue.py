"""Private reading queue and shared save/next-book helpers.

The public wishlist remains `reading_progress.status = 'wishlist'`.
The private queue is guild-scoped and never returned by shelf `query`.
"""
from __future__ import annotations

from datetime import datetime, timezone

from actions import (
    action_start_book,
    get_or_create_book,
    get_or_create_user,
    log_event,
    query_shelf,
)
from database import connect

# Discord select menus allow at most 25 options; keep both lists within that.
LIST_CAP = 25

SAVE_DESTINATIONS = frozenset({"queue", "wishlist"})
QUEUE_SOURCES = frozenset({"manual", "chat", "recommendation", "nomination"})


def _normalize_source(source: str | None) -> str | None:
    if not source:
        return None
    source = str(source).strip().lower()
    return source if source in QUEUE_SOURCES else "manual"


def action_queue_add(
    discord_id: str,
    display_name: str,
    guild_id: str,
    args: dict,
    *,
    source: str | None = None,
) -> tuple[bool, str]:
    """Add a book to the member's private queue.

    Returns (ok, status) where status is one of:
    added | already | full | missing_title
    """
    title = args.get("title")
    if not title:
        return False, "missing_title"
    if not guild_id:
        return False, "missing_title"
    author = args.get("author")
    resolved_source = _normalize_source(source or args.get("source") or "manual")

    conn = connect()
    try:
        user_id = get_or_create_user(conn, discord_id, display_name)
        book_id = get_or_create_book(conn, title, author)
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM reading_queue WHERE guild_id = ? AND user_id = ? AND book_id = ?",
            (guild_id, user_id, book_id),
        )
        if cur.fetchone():
            log_event(
                discord_id,
                guild_id,
                "queue_add",
                {"title": title, "author": author, "already_exists": True, "source": resolved_source},
            )
            return True, "already"

        cur.execute(
            "SELECT COUNT(*) FROM reading_queue WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        count = cur.fetchone()[0]
        if count >= LIST_CAP:
            log_event(
                discord_id,
                guild_id,
                "queue_add",
                {"title": title, "author": author, "full": True, "source": resolved_source},
            )
            return False, "full"

        cur.execute(
            """INSERT INTO reading_queue (user_id, guild_id, book_id, source)
               VALUES (?, ?, ?, ?)""",
            (user_id, guild_id, book_id, resolved_source),
        )
        conn.commit()
        log_event(
            discord_id,
            guild_id,
            "queue_add",
            {"title": title, "author": author, "source": resolved_source},
        )
        return True, "added"
    finally:
        conn.close()


def action_queue_remove(
    discord_id: str,
    display_name: str,
    guild_id: str,
    args: dict,
) -> tuple[bool, str]:
    """Remove a book from the private queue. Returns (ok, added|missing_title|not_found)."""
    title = args.get("title")
    if not title or not guild_id:
        return False, "missing_title"
    author = args.get("author")

    conn = connect()
    try:
        user_id = get_or_create_user(conn, discord_id, display_name)
        book_id = get_or_create_book(conn, title, author)
        cur = conn.cursor()
        cur.execute(
            """DELETE FROM reading_queue
               WHERE guild_id = ? AND user_id = ? AND book_id = ?""",
            (guild_id, user_id, book_id),
        )
        conn.commit()
        if cur.rowcount:
            log_event(discord_id, guild_id, "queue_remove", {"title": title, "author": author})
            return True, "removed"
        return False, "not_found"
    finally:
        conn.close()


def action_queue_list(discord_id: str, guild_id: str) -> list[dict]:
    """Return the member's private queue (oldest first). Never for another user."""
    if not guild_id:
        return []
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users WHERE discord_id = ?", (discord_id,))
        row = cur.fetchone()
        if not row:
            return []
        user_id = row[0]
        cur.execute(
            """SELECT b.title, b.author, rq.source, rq.created_at, rq.book_id
               FROM reading_queue rq
               JOIN books b ON rq.book_id = b.book_id
               WHERE rq.guild_id = ? AND rq.user_id = ?
               ORDER BY rq.created_at ASC, rq.queue_id ASC""",
            (guild_id, user_id),
        )
        return [
            {
                "title": r[0],
                "author": r[1],
                "source": r[2],
                "created_at": r[3],
                "book_id": r[4],
                "list": "queue",
            }
            for r in cur.fetchall()
        ]
    finally:
        conn.close()


def action_wishlist_remove(
    discord_id: str,
    display_name: str,
    guild_id: str,
    args: dict,
) -> tuple[bool, str]:
    """Remove a wishlist shelf row for this member in this guild."""
    title = args.get("title")
    if not title or not guild_id:
        return False, "missing_title"
    author = args.get("author")

    conn = connect()
    try:
        user_id = get_or_create_user(conn, discord_id, display_name)
        book_id = get_or_create_book(conn, title, author)
        cur = conn.cursor()
        cur.execute(
            """DELETE FROM reading_progress
               WHERE user_id = ? AND book_id = ? AND guild_id = ? AND status = 'wishlist'""",
            (user_id, book_id, guild_id),
        )
        conn.commit()
        if cur.rowcount:
            log_event(
                discord_id,
                guild_id,
                "remove_from_wishlist",
                {"title": title, "author": author},
            )
            return True, "removed"
        return False, "not_found"
    finally:
        conn.close()


def action_wishlist_add(
    discord_id: str,
    display_name: str,
    guild_id: str,
    args: dict,
    *,
    source: str | None = None,
) -> tuple[bool, str]:
    """Add to the public wishlist with optional source; enforces LIST_CAP per guild."""
    title = args.get("title")
    if not title or not guild_id:
        return False, "missing_title"
    author = args.get("author")
    resolved_source = _normalize_source(source or args.get("source") or "manual")

    existing = query_shelf(discord_id, "wishlist", guild_id)
    # Cap only counts current wishlist rows in this guild.
    if len(existing) >= LIST_CAP:
        # Still allow no-op if the title is already present.
        title_lower = title.lower()
        if any(item["title"].lower() == title_lower for item in existing):
            return True, "already"
        return False, "full"

    conn = connect()
    try:
        user_id = get_or_create_user(conn, discord_id, display_name)
        book_id = get_or_create_book(conn, title, author)
        cur = conn.cursor()
        cur.execute(
            """SELECT progress_id, status FROM reading_progress
               WHERE user_id = ? AND book_id = ? AND guild_id = ?
               ORDER BY progress_id DESC LIMIT 1""",
            (user_id, book_id, guild_id),
        )
        row = cur.fetchone()
        if row:
            log_event(
                discord_id,
                guild_id,
                "add_to_wishlist",
                {"title": title, "author": author, "already_exists": True, "source": resolved_source},
            )
            return True, "already"

        now = datetime.now(timezone.utc)
        cur.execute(
            """INSERT INTO reading_progress
               (user_id, book_id, guild_id, status, last_activity_at, source)
               VALUES (?, ?, ?, 'wishlist', ?, ?)""",
            (user_id, book_id, guild_id, now, resolved_source),
        )
        conn.commit()
        log_event(
            discord_id,
            guild_id,
            "add_to_wishlist",
            {"title": title, "author": author, "source": resolved_source},
        )
        return True, "added"
    finally:
        conn.close()


def action_save_book_to(
    destination: str,
    discord_id: str,
    display_name: str,
    guild_id: str,
    args: dict,
    *,
    source: str | None = None,
) -> tuple[bool, str]:
    """Save a book to `queue` or `wishlist`."""
    dest = (destination or "").strip().lower()
    if dest not in SAVE_DESTINATIONS:
        return False, "bad_destination"
    if dest == "queue":
        return action_queue_add(
            discord_id, display_name, guild_id, args, source=source or "manual"
        )
    return action_wishlist_add(
        discord_id, display_name, guild_id, args, source=source or "manual"
    )


def list_next_book_choices(discord_id: str, guild_id: str) -> list[dict]:
    """Combined labeled choices from private queue then public wishlist (max 25)."""
    choices: list[dict] = []
    for item in action_queue_list(discord_id, guild_id):
        choices.append(
            {
                "list": "queue",
                "title": item["title"],
                "author": item.get("author"),
                "label": f"Queue · {item['title']}",
            }
        )
    for item in query_shelf(discord_id, "wishlist", guild_id):
        choices.append(
            {
                "list": "wishlist",
                "title": item["title"],
                "author": item.get("author"),
                "label": f"Wishlist · {item['title']}",
            }
        )
    return choices[:LIST_CAP]


def action_start_from_choice(
    discord_id: str,
    display_name: str,
    guild_id: str,
    *,
    list_name: str,
    title: str,
    author: str | None = None,
) -> bool:
    """Start a book from the combined picker; drop queue rows when starting from queue."""
    args = {"title": title, "author": author}
    ok = action_start_book(discord_id, display_name, guild_id, args)
    if not ok:
        return False
    if list_name == "queue":
        action_queue_remove(discord_id, display_name, guild_id, args)
    return True


# Keep the classic wishlist helper available for callers that only need bool.
def action_add_to_wishlist_compat(
    discord_id: str, display_name: str, guild_id: str, args: dict
) -> bool:
    ok, _status = action_wishlist_add(discord_id, display_name, guild_id, args)
    return ok
