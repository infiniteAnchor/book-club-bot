"""Session-keyed scrapbook notes and book-level club aggregates."""
from __future__ import annotations

import re

from database import connect

SCRAPBOOK_KINDS = frozenset(
    {"quote", "highlight", "reaction", "rating_note", "discussion"}
)
MAX_CONTENT_LENGTH = 1000
MAX_ENTRIES_PER_SESSION = 100

_SESSION_FIELDS = (
    "session_id", "guild_id", "book_id", "status", "started_at", "target_date",
    "started_by", "poll_id", "completed_at", "created_at", "title", "author",
    "page_count",
)


def _valid_text_id(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _positive_id(value) -> int | None:
    # Snowflakes and database ids should not silently accept bools (bool is int).
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 < value <= 2**63 - 1 else None
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value.strip()):
        try:
            resolved = int(value.strip())
            return resolved if resolved <= 2**63 - 1 else None
        except ValueError:
            return None
    return None


def _session_dict(row) -> dict:
    return dict(zip(_SESSION_FIELDS, row))


def _fetch_sessions(cur, guild_id: str) -> list[dict]:
    cur.execute(
        """SELECT s.session_id, s.guild_id, s.book_id, s.status, s.started_at,
                  s.target_date, s.started_by, s.poll_id, s.completed_at, s.created_at,
                  b.title, b.author, b.page_count
           FROM reading_sessions s
           JOIN books b ON b.book_id = s.book_id
           WHERE s.guild_id = ?
           ORDER BY s.session_id DESC""",
        (guild_id,),
    )
    return [_session_dict(row) for row in cur.fetchall()]


def list_scrapbook_sessions(guild_id: str) -> list[dict]:
    """Return every session in this guild, newest first."""
    if not _valid_text_id(guild_id):
        return []
    conn = connect()
    try:
        return _fetch_sessions(conn.cursor(), guild_id.strip())
    finally:
        conn.close()


def _select_session(cur, guild_id: str, session_id=None, title=None):
    if session_id is not None:
        resolved_id = _positive_id(session_id)
        if resolved_id is None:
            return None
        cur.execute(
            """SELECT s.session_id, s.guild_id, s.book_id, s.status, s.started_at,
                      s.target_date, s.started_by, s.poll_id, s.completed_at, s.created_at,
                      b.title, b.author, b.page_count
               FROM reading_sessions s JOIN books b ON b.book_id = s.book_id
               WHERE s.guild_id = ? AND s.session_id = ?""",
            (guild_id, resolved_id),
        )
        row = cur.fetchone()
        return _session_dict(row) if row else None

    if title is not None:
        if not isinstance(title, str) or not title.strip():
            return None
        cur.execute(
            """SELECT s.session_id, s.guild_id, s.book_id, s.status, s.started_at,
                      s.target_date, s.started_by, s.poll_id, s.completed_at, s.created_at,
                      b.title, b.author, b.page_count
               FROM reading_sessions s JOIN books b ON b.book_id = s.book_id
               WHERE s.guild_id = ? AND LOWER(b.title) = LOWER(?)
               ORDER BY s.session_id DESC LIMIT 1""",
            (guild_id, title.strip()),
        )
    else:
        cur.execute(
            """SELECT s.session_id, s.guild_id, s.book_id, s.status, s.started_at,
                      s.target_date, s.started_by, s.poll_id, s.completed_at, s.created_at,
                      b.title, b.author, b.page_count
               FROM reading_sessions s JOIN books b ON b.book_id = s.book_id
               WHERE s.guild_id = ?
               ORDER BY CASE s.status WHEN 'active' THEN 0 ELSE 1 END,
                        s.session_id DESC LIMIT 1""",
            (guild_id,),
        )
    row = cur.fetchone()
    return _session_dict(row) if row else None


def _book_aggregates(cur, guild_id: str, book_id: int) -> tuple[dict, int]:
    # These are club lifetime aggregates for the book in this guild, matching
    # /history. Goodreads-imported history and ratings stay personal-only.
    cur.execute(
        """SELECT COUNT(*) FROM reading_progress rp
           WHERE rp.guild_id = ? AND rp.book_id = ? AND rp.status = 'finished'
             AND (rp.source IS NULL OR rp.source != 'goodreads')""",
        (guild_id, book_id),
    )
    finishes = cur.fetchone()[0]
    cur.execute(
        """SELECT COUNT(r.rating_id), ROUND(AVG(r.score), 1)
           FROM ratings r
           WHERE r.book_id = ?
             AND (r.source IS NULL OR r.source != 'goodreads')
             AND EXISTS (
                 SELECT 1 FROM reading_progress rp
                 WHERE rp.user_id = r.user_id AND rp.book_id = r.book_id
                   AND rp.guild_id = ? AND rp.status = 'finished'
                   AND (rp.source IS NULL OR rp.source != 'goodreads')
             )""",
        (book_id, guild_id),
    )
    count, average = cur.fetchone()
    return {"count": count, "average": average}, finishes


def get_scrapbook(guild_id: str, session_id=None, title=None) -> dict | None:
    """Return one session's entries plus guild lifetime book aggregates."""
    if not _valid_text_id(guild_id):
        return None
    conn = connect()
    try:
        cur = conn.cursor()
        session = _select_session(cur, guild_id.strip(), session_id, title)
        if not session:
            return None
        cur.execute(
            """SELECT entry_id, session_id, discord_id, display_name, kind, content, created_at
               FROM scrapbook_entries WHERE session_id = ?
               ORDER BY created_at, entry_id""",
            (session["session_id"],),
        )
        entries = [
            dict(zip(("entry_id", "session_id", "discord_id", "display_name", "kind", "content", "created_at"), row))
            for row in cur.fetchall()
        ]
        ratings, finishes = _book_aggregates(cur, guild_id.strip(), session["book_id"])
        return {"session": session, "entries": entries, "ratings": ratings, "finishes": finishes}
    finally:
        conn.close()


def add_scrapbook_entry(
    guild_id: str, discord_id: str, display_name: str, args: dict
) -> tuple[bool, str, dict | None]:
    """Append one bounded note. Status: added, no_session, invalid_content,
    invalid_kind, invalid_session, or full.
    """
    if not _valid_text_id(guild_id) or not _valid_text_id(discord_id):
        return False, "no_session", None
    if not isinstance(args, dict):
        return False, "invalid_content", None
    content = args.get("content")
    if not isinstance(content, str) or not content.strip() or len(content) > MAX_CONTENT_LENGTH:
        return False, "invalid_content", None
    kind = args.get("kind", "highlight")
    if not isinstance(kind, str) or kind not in SCRAPBOOK_KINDS:
        return False, "invalid_kind", None
    explicit_id = args.get("session_id")
    if explicit_id is not None and _positive_id(explicit_id) is None:
        return False, "invalid_session", None
    title = args.get("title")
    if explicit_id is None and title is not None and (not isinstance(title, str) or not title.strip()):
        return False, "no_session", None

    conn = connect()
    try:
        # Serialize count+insert so concurrent submissions cannot exceed cap.
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.cursor()
        session = _select_session(cur, guild_id.strip(), explicit_id, title)
        if not session:
            conn.rollback()
            return False, "invalid_session" if explicit_id is not None else "no_session", None
        cur.execute(
            "SELECT COUNT(*) FROM scrapbook_entries WHERE session_id = ?",
            (session["session_id"],),
        )
        if cur.fetchone()[0] >= MAX_ENTRIES_PER_SESSION:
            conn.rollback()
            return False, "full", None
        name = display_name.strip() if isinstance(display_name, str) and display_name.strip() else "A member"
        cur.execute(
            """INSERT INTO scrapbook_entries
               (session_id, discord_id, display_name, kind, content)
               VALUES (?, ?, ?, ?, ?)""",
            (session["session_id"], discord_id.strip(), name[:100], kind, content.strip()),
        )
        entry_id = cur.lastrowid
        cur.execute(
            """SELECT entry_id, session_id, discord_id, display_name, kind, content, created_at
               FROM scrapbook_entries WHERE entry_id = ?""",
            (entry_id,),
        )
        row = cur.fetchone()
        conn.commit()
        entry = dict(zip(("entry_id", "session_id", "discord_id", "display_name", "kind", "content", "created_at"), row))
        return True, "added", entry
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def remove_scrapbook_entry(guild_id: str, discord_id: str, entry_id) -> bool:
    """Delete only the caller's own entry within the requested guild."""
    if not _valid_text_id(guild_id) or not _valid_text_id(discord_id):
        return False
    resolved_id = _positive_id(entry_id)
    if resolved_id is None:
        return False
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """DELETE FROM scrapbook_entries
               WHERE entry_id = ? AND discord_id = ?
                 AND session_id IN (
                     SELECT session_id FROM reading_sessions WHERE guild_id = ?
                 )""",
            (resolved_id, discord_id.strip(), guild_id.strip()),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()
