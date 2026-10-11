"""Canonical shared reading session: one current club book per guild."""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone

from actions import get_or_create_book, log_event
from database import connect

SESSION_ACTIVE = "active"
SESSION_COMPLETED = "completed"

_CLEAR_TARGET_WORDS = frozenset({"", "clear", "none", "remove", "unset"})
_RELATIVE_TARGET = re.compile(
    r"^in\s+(\d+)\s+(day|days|week|weeks)$",
    re.IGNORECASE,
)
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%m/%d/%Y",
    "%B %d, %Y",
    "%B %d %Y",
    "%b %d, %Y",
    "%b %d %Y",
    "%d %B %Y",
    "%d %b %Y",
)


def parse_target_date(value, *, now: datetime | None = None) -> tuple[str | None, str]:
    """Parse a club target into YYYY-MM-DD.

    Returns (date, status) where status is ok, cleared, or invalid.
    """
    if value is None:
        return None, "invalid"
    if isinstance(value, datetime):
        return value.date().isoformat(), "ok"
    text = str(value).strip()
    if text.lower() in _CLEAR_TARGET_WORDS:
        return None, "cleared"

    clock = now or datetime.now(timezone.utc)
    if text.lower() == "tomorrow":
        return (clock + timedelta(days=1)).date().isoformat(), "ok"
    relative = _RELATIVE_TARGET.fullmatch(text)
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2).lower()
        days = amount * (7 if unit.startswith("week") else 1)
        try:
            return (clock + timedelta(days=days)).date().isoformat(), "ok"
        except OverflowError:
            return None, "invalid"

    iso_candidate = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso_candidate)
        return parsed.date().isoformat(), "ok"
    except ValueError:
        pass

    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat(), "ok"
        except ValueError:
            continue
    return None, "invalid"


def _row_to_session(row) -> dict:
    return {
        "session_id": row[0],
        "guild_id": row[1],
        "book_id": row[2],
        "status": row[3],
        "started_at": row[4],
        "target_date": row[5],
        "started_by": row[6],
        "poll_id": row[7],
        "completed_at": row[8],
        "title": row[9],
        "author": row[10],
        "page_count": row[11],
    }


def _fetch_active(cur, guild_id: str) -> dict | None:
    cur.execute(
        """SELECT s.session_id, s.guild_id, s.book_id, s.status, s.started_at, s.target_date,
                  s.started_by, s.poll_id, s.completed_at, b.title, b.author, b.page_count
           FROM reading_sessions s
           JOIN books b ON b.book_id = s.book_id
           WHERE s.guild_id = ? AND s.status = ?
           ORDER BY s.session_id DESC LIMIT 1""",
        (guild_id, SESSION_ACTIVE),
    )
    row = cur.fetchone()
    return _row_to_session(row) if row else None


def _fetch_session(cur, session_id: int) -> dict | None:
    cur.execute(
        """SELECT s.session_id, s.guild_id, s.book_id, s.status, s.started_at, s.target_date,
                  s.started_by, s.poll_id, s.completed_at, b.title, b.author, b.page_count
           FROM reading_sessions s
           JOIN books b ON b.book_id = s.book_id
           WHERE s.session_id = ?""",
        (session_id,),
    )
    row = cur.fetchone()
    return _row_to_session(row) if row else None


def get_active_session(guild_id: str) -> dict | None:
    if not guild_id:
        return None
    conn = connect()
    try:
        return _fetch_active(conn.cursor(), guild_id)
    finally:
        conn.close()


def list_session_progress(guild_id: str, book_id: int) -> list[dict]:
    """Member progress on the club book in this guild. Wishlist rows are omitted."""
    if not guild_id or not book_id:
        return []
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT u.display_name, rp.status, rp.progress_pct, rp.progress_raw, rp.progress_unit
               FROM reading_progress rp
               JOIN users u ON u.user_id = rp.user_id
               WHERE rp.guild_id = ? AND rp.book_id = ? AND rp.status IN ('reading', 'finished', 'dnf')
               ORDER BY CASE rp.status
                            WHEN 'reading' THEN 0
                            WHEN 'finished' THEN 1
                            ELSE 2
                        END,
                        u.display_name COLLATE NOCASE ASC""",
            (guild_id, book_id),
        )
        return [
            {
                "display_name": row[0] or "A member",
                "status": row[1],
                "progress_pct": row[2],
                "progress_raw": row[3],
                "progress_unit": row[4],
            }
            for row in cur.fetchall()
        ]
    finally:
        conn.close()


def get_current_read(guild_id: str) -> dict | None:
    session = get_active_session(guild_id)
    if not session:
        return None
    session["members"] = list_session_progress(guild_id, session["book_id"])
    return session


def _resolve_book(
    conn, args: dict, *, create: bool = True
) -> tuple[int, str, str | None] | None:
    cur = conn.cursor()
    book_id = args.get("book_id")
    title = (args.get("title") or "").strip()
    author = (args.get("author") or "").strip() or None
    if book_id:
        cur.execute("SELECT book_id, title, author FROM books WHERE book_id = ?", (book_id,))
        row = cur.fetchone()
        if row:
            return row[0], row[1], row[2]
    if not title:
        return None
    if not create:
        cur.execute(
            "SELECT book_id, title, author FROM books WHERE LOWER(title) = LOWER(?)",
            (title,),
        )
        row = cur.fetchone()
        return (row[0], row[1], row[2]) if row else None
    resolved_id = get_or_create_book(conn, title, author)
    cur.execute("SELECT book_id, title, author FROM books WHERE book_id = ?", (resolved_id,))
    row = cur.fetchone()
    if not row:
        return None
    return row[0], row[1], row[2]


def _complete_active(cur, session_id: int, now: str) -> None:
    cur.execute(
        """UPDATE reading_sessions
           SET status = ?, completed_at = ?
           WHERE session_id = ? AND status = ?""",
        (SESSION_COMPLETED, now, session_id, SESSION_ACTIVE),
    )


def start_club_read(
    guild_id: str,
    started_by: str | None,
    args: dict,
    *,
    replace: bool = False,
    poll_id: int | None = None,
) -> tuple[bool, str, dict | None]:
    """Start (or replace) the guild's current club book.

    Status: started, replaced, already, already_active, missing_title,
    missing_guild, invalid_date.
    """
    if not guild_id:
        return False, "missing_guild", None
    target_raw = args.get("target_date", args.get("date", args.get("target", None)))
    target_date = None
    if target_raw not in (None,):
        parsed, parse_status = parse_target_date(target_raw)
        if parse_status == "invalid":
            return False, "invalid_date", None
        if parse_status == "ok":
            target_date = parsed

    conn = connect()
    try:
        cur = conn.cursor()
        existing = _fetch_active(cur, guild_id)
        if existing and not replace:
            book = _resolve_book(conn, args, create=False)
            if book and existing["book_id"] == book[0]:
                return True, "already", existing
            if not book and not (args.get("title") or "").strip():
                return False, "missing_title", None
            return False, "already_active", existing
        book = _resolve_book(conn, args)
        if book is None:
            return False, "missing_title", None
        book_id, title, author = book
        if existing and existing["book_id"] == book_id:
            return True, "already", existing
        now = datetime.now(timezone.utc).isoformat()
        if existing and replace:
            _complete_active(cur, existing["session_id"], now)
        try:
            cur.execute(
                """INSERT INTO reading_sessions
                   (guild_id, book_id, status, started_at, target_date, started_by, poll_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    guild_id,
                    book_id,
                    SESSION_ACTIVE,
                    now,
                    target_date,
                    started_by,
                    poll_id,
                ),
            )
        except sqlite3.IntegrityError:
            conn.rollback()
            current = _fetch_active(cur, guild_id)
            return False, "already_active", current
        session_id = cur.lastrowid
        conn.commit()
        session = _fetch_session(cur, session_id) if session_id else None
        log_event(
            started_by,
            guild_id,
            "start_club_read",
            {
                "title": title,
                "author": author,
                "book_id": book_id,
                "replace": bool(existing and replace),
                "poll_id": poll_id,
                "target_date": target_date,
            },
        )
        status = "replaced" if existing and replace else "started"
        return True, status, session
    finally:
        conn.close()


def set_club_target(
    guild_id: str, discord_id: str | None, target_raw
) -> tuple[bool, str, dict | None]:
    """Set or clear the active session's optional target date."""
    if not guild_id:
        return False, "missing_guild", None
    parsed, parse_status = parse_target_date(target_raw)
    if parse_status == "invalid":
        return False, "invalid_date", None
    conn = connect()
    try:
        cur = conn.cursor()
        session = _fetch_active(cur, guild_id)
        if not session:
            return False, "no_session", None
        target_date = parsed if parse_status == "ok" else None
        cur.execute(
            "UPDATE reading_sessions SET target_date = ? WHERE session_id = ?",
            (target_date, session["session_id"]),
        )
        conn.commit()
        session["target_date"] = target_date
        log_event(
            discord_id,
            guild_id,
            "set_club_target",
            {"session_id": session["session_id"], "target_date": target_date},
        )
        status = "cleared" if parse_status == "cleared" else "set"
        return True, status, session
    finally:
        conn.close()


def complete_club_read(
    guild_id: str, discord_id: str | None = None
) -> tuple[bool, str, dict | None]:
    if not guild_id:
        return False, "missing_guild", None
    conn = connect()
    try:
        cur = conn.cursor()
        session = _fetch_active(cur, guild_id)
        if not session:
            return False, "no_session", None
        now = datetime.now(timezone.utc).isoformat()
        _complete_active(cur, session["session_id"], now)
        conn.commit()
        session["status"] = SESSION_COMPLETED
        session["completed_at"] = now
        log_event(
            discord_id,
            guild_id,
            "complete_club_read",
            {"session_id": session["session_id"], "book_id": session["book_id"]},
        )
        return True, "completed", session
    finally:
        conn.close()


def maybe_start_from_poll_winner(
    guild_id: str,
    started_by: str | None,
    winner: dict | None,
    poll_id: int | None = None,
) -> dict | None:
    """Start a session from a poll winner when the guild has no current read."""
    if not guild_id or not winner:
        return None
    if get_active_session(guild_id):
        return None
    args = {
        "title": winner.get("title"),
        "author": winner.get("author"),
        "book_id": winner.get("book_id"),
    }
    ok, _status, session = start_club_read(
        guild_id, started_by, args, poll_id=poll_id
    )
    return session if ok else None


def _start_lookup_needed(bound: dict) -> bool:
    guild_id = bound.get("guild_id")
    if not guild_id:
        return False
    args = bound.get("args") or {}
    target_raw = args.get("target_date", args.get("date", args.get("target", None)))
    if target_raw is not None and parse_target_date(target_raw)[1] == "invalid":
        return False
    return bool(bound.get("replace") or not get_active_session(guild_id))


start_club_read.book_lookup_needed = _start_lookup_needed


def _winner_lookup_needed(bound: dict) -> bool:
    guild_id = bound.get("guild_id")
    return bool(guild_id and bound.get("winner") and not get_active_session(guild_id))


maybe_start_from_poll_winner.book_lookup_needed = _winner_lookup_needed
