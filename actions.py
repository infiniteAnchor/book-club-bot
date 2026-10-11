import urllib.request
import urllib.parse
import json as _json
import asyncio
import inspect
from contextvars import ContextVar
from datetime import datetime, timezone, timedelta
from database import connect

GOODREADS_SOURCE = "goodreads"
_prefetched_books: ContextVar[dict | None] = ContextVar("prefetched_books", default=None)


async def run_book_action(fn, *args, **kwargs):
    """Fetch optional book metadata off-loop, then run the DB action on-loop.

    The action keeps its synchronous API and its SQLite work in the caller's
    task. A scoped cache also prevents nested actions from making HTTP calls.
    """
    bound = inspect.signature(fn).bind_partial(*args, **kwargs).arguments
    book_args = bound.get("args") if isinstance(bound.get("args"), dict) else bound
    if isinstance(bound.get("winner"), dict):
        book_args = bound["winner"]
    title = book_args.get("title")
    author = book_args.get("author")
    metadata = {}
    preflight = getattr(fn, "book_lookup_needed", None)
    lookup_needed = preflight(bound) if preflight else True
    if lookup_needed and isinstance(title, str) and title.strip():
        conn = connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM books WHERE LOWER(title) = LOWER(?)", (title,)
            ).fetchone()
        finally:
            conn.close()
        if not row:
            metadata = await asyncio.to_thread(lookup_google_books, title, author)
    token = _prefetched_books.set({title.strip().lower(): metadata} if isinstance(title, str) else {})
    try:
        return fn(*args, **kwargs)
    finally:
        _prefetched_books.reset(token)


def log_event(discord_id: str | None, guild_id: str | None, event_type: str, payload: dict | None = None):
    """
    Insert a row into the event_log table.
    If discord_id is not found in users, user_id is stored as None.
    """
    conn = connect()
    try:
        cur = conn.cursor()
        user_id = None
        if discord_id:
            cur.execute("SELECT user_id FROM users WHERE discord_id = ?", (discord_id,))
            row = cur.fetchone()
            user_id = row[0] if row else None
        cur.execute(
            "INSERT INTO event_log (user_id, guild_id, event_type, payload) VALUES (?, ?, ?, ?)",
            (user_id, guild_id, event_type, _json.dumps(payload or {}))
        )
        conn.commit()
    finally:
        conn.close()


def record_reading_activity(cur, user_id: int, guild_id: str | None, when: datetime) -> None:
    """Mark the UTC day of `when` as a reading day for streaks; repeats are no-ops."""
    if not guild_id:
        return
    cur.execute(
        "INSERT OR IGNORE INTO reading_activity (user_id, guild_id, activity_date) VALUES (?, ?, ?)",
        (user_id, guild_id, when.astimezone(timezone.utc).date().isoformat())
    )


def get_access_tier(discord_id: str) -> str:
    """
    Returns the access_tier for a user, or 'core' if the user is not found.
    """
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT access_tier FROM users WHERE discord_id = ?", (discord_id,))
        row = cur.fetchone()
        return row[0] if row else 'core'
    finally:
        conn.close()


def lookup_google_books(title: str, author: str | None = None) -> dict:
    """
    Look up a book on Google Books by title (and optionally author).
    Returns a dict with author, page_count, genre if a confident match is found.
    Returns empty dict if no match or on any error.
    """
    try:
        from config import GOOGLE_BOOKS_API_KEY
        query = f"intitle:{urllib.parse.quote(title)}"
        if author:
            query += f"+inauthor:{urllib.parse.quote(author)}"
        key_param = f"&key={GOOGLE_BOOKS_API_KEY}" if GOOGLE_BOOKS_API_KEY else ""
        url = f"https://www.googleapis.com/books/v1/volumes?q={query}&maxResults=3{key_param}"
        req = urllib.request.Request(url, headers={"User-Agent": "BookClubBot/1.0"})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = _json.loads(response.read())

        items = data.get("items", [])
        if not items:
            return {}

        title_lower = title.lower().strip()
        for item in items:
            info = item.get("volumeInfo", {})
            candidate = info.get("title", "").lower().strip()
            if title_lower not in candidate and candidate not in title_lower:
                continue
            result = {}
            authors = info.get("authors", [])
            if authors:
                result["author"] = ", ".join(authors)
            page_count = info.get("pageCount")
            if page_count and 50 < int(page_count) < 5000:
                result["page_count"] = int(page_count)
            cats = info.get("categories", [])
            if cats:
                result["genre"] = cats[0][:50]
            return result

        return {}
    except Exception:
        return {}


def get_or_create_user(conn, discord_id: str, display_name: str) -> int:
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE discord_id = ?", (discord_id,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        "INSERT INTO users (discord_id, display_name) VALUES (?, ?)",
        (discord_id, display_name)
    )
    conn.commit()
    return cur.lastrowid


def get_or_create_book(
    conn,
    title: str,
    author: str | None = None,
    *,
    enrich: bool = True,
    commit: bool = True,
) -> int:
    cur = conn.cursor()
    cur.execute(
        "SELECT book_id FROM books WHERE LOWER(title) = LOWER(?)",
        (title,)
    )
    row = cur.fetchone()
    if row:
        return row[0]

    prefetched = _prefetched_books.get()
    if not enrich:
        gb_data = {}
    elif prefetched is not None:
        # A wrapped action never performs network I/O on the event loop.
        gb_data = prefetched.get(title.strip().lower(), {})
    else:
        gb_data = lookup_google_books(title, author)
    final_author = author or gb_data.get("author")
    page_count = gb_data.get("page_count")
    genre = gb_data.get("genre")

    cur.execute(
        "INSERT INTO books (title, author, page_count, genre) VALUES (?, ?, ?, ?)",
        (title, final_author, page_count, genre)
    )
    if commit:
        conn.commit()
    return cur.lastrowid


def _parse_db_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def get_user_book_progress(discord_id: str, title: str, guild_id: str, author: str | None = None) -> dict | None:
    """
    Returns the most recent progress row for a user's book in this guild, or None.
    """
    if not guild_id:
        return None
    conn = connect()
    try:
        cur = conn.cursor()
        query = [
            """SELECT rp.progress_id, rp.status, b.title, b.author, rp.started_at, rp.finished_at,
                      rp.last_activity_at, rp.last_nudged_at
               FROM reading_progress rp
               JOIN books b ON rp.book_id = b.book_id
               JOIN users u ON rp.user_id = u.user_id
               WHERE u.discord_id = ? AND LOWER(b.title) = LOWER(?) AND rp.guild_id = ?"""
        ]
        params = [discord_id, title, guild_id]
        if author:
            query.append("AND LOWER(COALESCE(b.author, '')) = LOWER(?)")
            params.append(author)
        query.append("ORDER BY rp.progress_id DESC LIMIT 1")
        cur.execute(" ".join(query), params)
        row = cur.fetchone()
        if not row:
            return None
        return {
            "progress_id": row[0],
            "status": row[1],
            "title": row[2],
            "author": row[3],
            "started_at": row[4],
            "finished_at": row[5],
            "last_activity_at": row[6],
            "last_nudged_at": row[7],
        }
    finally:
        conn.close()


def _latest_progress_row(cur, user_id, book_id, guild_id, *, status: str | None = None):
    """Newest reading_progress row for this user, book, and guild."""
    sql = """SELECT progress_id, status, source FROM reading_progress
             WHERE user_id = ? AND book_id = ? AND guild_id = ?"""
    params = [user_id, book_id, guild_id]
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY progress_id DESC LIMIT 1"
    cur.execute(sql, params)
    return cur.fetchone()


def action_add_book(discord_id: str, display_name: str, guild_id: str, args: dict) -> bool:
    """Add to the guild wishlist; same cap and guild filter as action_wishlist_add."""
    return action_add_to_wishlist(discord_id, display_name, guild_id, args)


def action_group_query(guild_id: str) -> list:
    """
    Returns a list of all currently reading books for all users in the guild.
    """
    if not guild_id:
        return []
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT u.display_name, b.title, b.author, rp.progress_raw, rp.progress_unit
               FROM reading_progress rp
               JOIN users u ON rp.user_id = u.user_id
               JOIN books b ON rp.book_id = b.book_id
               WHERE rp.guild_id = ? AND rp.status = 'reading'
               ORDER BY u.display_name ASC""",
            (guild_id,)
        )
        return [
            {
                "user": r[0],
                "title": r[1],
                "author": r[2],
                "progress_raw": r[3],
                "progress_unit": r[4]
            }
            for r in cur.fetchall()
        ]
    finally:
        conn.close()


def action_start_book(discord_id: str, display_name: str, guild_id: str, args: dict) -> bool:
    title = args.get("title")
    if not title or not guild_id:
        return False
    author = args.get("author")
    conn = connect()
    try:
        user_id = get_or_create_user(conn, discord_id, display_name)
        book_id = get_or_create_book(conn, title, author)
        cur = conn.cursor()
        now = datetime.now(timezone.utc)
        existing = _latest_progress_row(cur, user_id, book_id, guild_id)
        if existing:
            progress_id, status, source = existing
            if status == 'wishlist':
                # Move wishlist entry to reading
                cur.execute(
                    """UPDATE reading_progress
                       SET status = 'reading', started_at = ?, last_activity_at = ?, last_nudged_at = NULL
                       WHERE progress_id = ?""",
                    (now, now, progress_id)
                )
                record_reading_activity(cur, user_id, guild_id, now)
                conn.commit()
                log_event(discord_id, guild_id, "start_book", {"title": title, "author": author, "moved_from_wishlist": True})
                return True
            if status == 'finished' and source == GOODREADS_SOURCE:
                cur.execute(
                    """INSERT INTO reading_progress
                       (user_id, book_id, guild_id, status, started_at, last_activity_at)
                       VALUES (?, ?, ?, 'reading', ?, ?)""",
                    (user_id, book_id, guild_id, now, now)
                )
                record_reading_activity(cur, user_id, guild_id, now)
                conn.commit()
                log_event(discord_id, guild_id, "start_book", {"title": title, "author": author})
                return True
            # If already reading or Discord-finished, do nothing
            return True
        # Not on any shelf yet — create as reading
        cur.execute(
            """INSERT INTO reading_progress
               (user_id, book_id, guild_id, status, started_at, last_activity_at)
               VALUES (?, ?, ?, 'reading', ?, ?)""",
            (user_id, book_id, guild_id, now, now)
        )
        record_reading_activity(cur, user_id, guild_id, now)
        conn.commit()
        log_event(discord_id, guild_id, "start_book", {"title": title, "author": author})
        return True
    finally:
        conn.close()


def action_update_progress(discord_id: str, display_name: str, guild_id: str, args: dict) -> bool:
    title = args.get("title")
    if not title or not guild_id:
        return False
    progress_raw = args.get("progress_raw") or args.get("progress")
    progress_unit = args.get("progress_unit") or args.get("unit", "percent")
    note = args.get("note")
    conn = connect()
    try:
        user_id = get_or_create_user(conn, discord_id, display_name)
        book_id = get_or_create_book(conn, title)
        cur = conn.cursor()
        now = datetime.now(timezone.utc)
        row = _latest_progress_row(cur, user_id, book_id, guild_id, status="reading")
        if not row:
            return False  # Can't update progress on a book not being read
        progress_id = row[0]

        # Compute progress_pct from page_count if available
        progress_pct = None
        if progress_raw is not None:
            try:
                raw_val = int(progress_raw)
            except (TypeError, ValueError):
                raw_val = None
            if raw_val is not None:
                cur.execute("SELECT page_count FROM books WHERE book_id = ?", (book_id,))
                page_row = cur.fetchone()
                page_count = page_row[0] if page_row else None
                unit_lower = (progress_unit or "").lower()
                if page_count and page_count > 0:
                    if unit_lower in ("page", "pages"):
                        progress_pct = round((raw_val / page_count) * 100)
                    elif unit_lower in ("percent", "percentage", "%"):
                        progress_pct = raw_val
                elif unit_lower in ("percent", "percentage", "%"):
                    progress_pct = raw_val

        cur.execute(
            """UPDATE reading_progress
               SET progress_raw = ?, progress_unit = ?, progress_pct = ?, notes = ?, last_activity_at = ?, last_nudged_at = NULL
               WHERE progress_id = ?""",
            (progress_raw, progress_unit, progress_pct, note, now, progress_id)
        )
        record_reading_activity(cur, user_id, guild_id, now)
        conn.commit()
        log_event(discord_id, guild_id, "update_progress", {
            "title": args.get("title"),
            "author": args.get("author"),
            "progress_raw": progress_raw,
            "progress_unit": progress_unit,
        })
        return True
    finally:
        conn.close()


def action_add_to_wishlist(discord_id: str, display_name: str, guild_id: str, args: dict) -> bool:
    from actions_queue import action_wishlist_add
    ok, _status = action_wishlist_add(discord_id, display_name, guild_id, args)
    return ok


def action_finish_book(discord_id: str, display_name: str, guild_id: str, args: dict) -> bool:
    title = args.get("title")
    if not title or not guild_id:
        return False
    author = args.get("author")
    conn = connect()
    try:
        user_id = get_or_create_user(conn, discord_id, display_name)
        book_id = get_or_create_book(conn, title, author)
        cur = conn.cursor()
        now = datetime.now(timezone.utc)
        existing = _latest_progress_row(cur, user_id, book_id, guild_id)
        if existing:
            progress_id, status, _source = existing
            if status == 'finished':
                # Re-read — create a new entry and increment read_count
                cur.execute(
                    "SELECT read_count FROM reading_progress WHERE progress_id = ?",
                    (progress_id,)
                )
                read_count = cur.fetchone()[0] + 1
                cur.execute(
                    """INSERT INTO reading_progress
                       (user_id, book_id, guild_id, status, read_count, started_at, finished_at, last_activity_at)
                       VALUES (?, ?, ?, 'finished', ?, ?, ?, ?)""",
                    (user_id, book_id, guild_id, read_count, now, now, now)
                )
            else:
                # Move current entry to finished
                cur.execute(
                    """UPDATE reading_progress
                       SET status = 'finished', finished_at = ?, last_activity_at = ?, last_nudged_at = NULL
                       WHERE progress_id = ?""",
                    (now, now, progress_id)
                )
        else:
            # Not tracked at all — add and immediately finish
            cur.execute(
                """INSERT INTO reading_progress
                   (user_id, book_id, guild_id, status, started_at, finished_at, last_activity_at)
                   VALUES (?, ?, ?, 'finished', ?, ?, ?)""",
                (user_id, book_id, guild_id, now, now, now)
            )
        record_reading_activity(cur, user_id, guild_id, now)
        conn.commit()
        log_event(discord_id, guild_id, "finish_book", {"title": title, "author": author})
        return True
    finally:
        conn.close()


def action_dnf_book(discord_id: str, display_name: str, guild_id: str, args: dict):
    """
    Marks a book as DNF (did not finish). Returns (success, title, author).
    """
    title = args.get("title")
    if not title:
        return False, None, None
    author = args.get("author")
    conn = connect()
    try:
        user_id = get_or_create_user(conn, discord_id, display_name)
        book_id = get_or_create_book(conn, title, author)
        cur = conn.cursor()
        now = datetime.now(timezone.utc)
        cur.execute(
            """SELECT progress_id, status, source FROM reading_progress
               WHERE user_id = ? AND book_id = ? AND guild_id = ?
               ORDER BY progress_id DESC LIMIT 1""",
            (user_id, book_id, guild_id)
        )
        existing = cur.fetchone()
        if existing:
            progress_id, status, source = existing
            if status == 'dnf':
                return False, title, author
            if source == GOODREADS_SOURCE:
                cur.execute(
                    """INSERT INTO reading_progress
                       (user_id, book_id, guild_id, status, started_at, finished_at, last_activity_at)
                       VALUES (?, ?, ?, 'dnf', ?, ?, ?)""",
                    (user_id, book_id, guild_id, now, now, now)
                )
            else:
                cur.execute(
                    """UPDATE reading_progress
                       SET status = 'dnf', finished_at = ?, last_activity_at = ?, last_nudged_at = NULL
                       WHERE progress_id = ?""",
                    (now, now, progress_id)
                )
        else:
            cur.execute(
                """INSERT INTO reading_progress
                   (user_id, book_id, guild_id, status, started_at, finished_at, last_activity_at)
                   VALUES (?, ?, ?, 'dnf', ?, ?, ?)""",
                (user_id, book_id, guild_id, now, now, now)
            )
        record_reading_activity(cur, user_id, guild_id, now)
        conn.commit()
        # Fetch canonical title/author from DB for the eulogy
        cur.execute("SELECT title, author FROM books WHERE book_id = ?", (book_id,))
        row = cur.fetchone()
        if row:
            log_event(discord_id, guild_id, "dnf_book", {"title": row[0], "author": row[1]})
        return True, row[0] if row else title, row[1] if row else author
    finally:
        conn.close()


def action_rate_book(discord_id: str, display_name: str, args: dict, guild_id: str | None = None) -> bool:
    title = args.get("title")
    score = args.get("score") or args.get("rating")
    if not title or score is None:
        return False
    try:
        score = int(score)
    except (TypeError, ValueError):
        return False
    if not 1 <= score <= 10:
        return False
    review_notes = args.get("note") or args.get("review_notes")
    conn = connect()
    try:
        user_id = get_or_create_user(conn, discord_id, display_name)
        book_id = get_or_create_book(conn, title)
        cur = conn.cursor()
        # Upsert — if already rated, update it
        cur.execute(
            "SELECT rating_id FROM ratings WHERE user_id = ? AND book_id = ?",
            (user_id, book_id)
        )
        existing = cur.fetchone()
        if existing:
            cur.execute(
                """UPDATE ratings SET score = ?, review_notes = ?, rated_at = ?, source = NULL
                   WHERE rating_id = ?""",
                (score, review_notes, datetime.now(timezone.utc), existing[0])
            )
        else:
            cur.execute(
                """INSERT INTO ratings (user_id, book_id, score, review_notes)
                   VALUES (?, ?, ?, ?)""",
                (user_id, book_id, score, review_notes)
            )
        conn.commit()
        log_event(discord_id, guild_id, "rate_book", {"title": title, "author": args.get("author"), "score": score})
        return True
    finally:
        conn.close()


def query_shelf(discord_id: str, shelf: str, guild_id: str | None = None) -> list:
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users WHERE discord_id = ?", (discord_id,))
        row = cur.fetchone()
        if not row:
            return []
        user_id = row[0]
        query = """SELECT b.title, b.author, rp.progress_raw, rp.progress_unit,
                          rp.finished_at, rp.started_at
                   FROM reading_progress rp
                   JOIN books b ON rp.book_id = b.book_id
                   WHERE rp.user_id = ? AND rp.status = ?"""
        params = [user_id, shelf]
        if guild_id:
            query += " AND rp.guild_id = ?"
            params.append(guild_id)
        query += " ORDER BY rp.progress_id DESC"
        cur.execute(query, params)
        return [
            {
                "title": r[0],
                "author": r[1],
                "progress_raw": r[2],
                "progress_unit": r[3],
                "finished_at": r[4],
                "started_at": r[5]
            }
            for r in cur.fetchall()
        ]
    finally:
        conn.close()


def get_user_reading_history(discord_id: str, guild_id: str) -> dict:
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users WHERE discord_id = ?", (discord_id,))
        row = cur.fetchone()
        if not row:
            return {"finished": [], "reading": [], "wishlist": []}
        user_id = row[0]

        cur.execute("""
            SELECT b.title, b.author, r.score
            FROM reading_progress rp
            JOIN books b ON rp.book_id = b.book_id
            LEFT JOIN ratings r ON r.user_id = rp.user_id AND r.book_id = rp.book_id
            WHERE rp.user_id = ? AND rp.guild_id = ? AND rp.status = 'finished'
            ORDER BY rp.finished_at DESC
        """, (user_id, guild_id))
        finished = [{"title": r[0], "author": r[1], "rating": r[2]} for r in cur.fetchall()]

        cur.execute("""
            SELECT b.title, b.author
            FROM reading_progress rp
            JOIN books b ON rp.book_id = b.book_id
            WHERE rp.user_id = ? AND rp.guild_id = ? AND rp.status = 'reading'
        """, (user_id, guild_id))
        reading = [{"title": r[0], "author": r[1]} for r in cur.fetchall()]

        cur.execute("""
            SELECT b.title, b.author
            FROM reading_progress rp
            JOIN books b ON rp.book_id = b.book_id
            WHERE rp.user_id = ? AND rp.guild_id = ? AND rp.status = 'wishlist'
        """, (user_id, guild_id))
        wishlist = [{"title": r[0], "author": r[1]} for r in cur.fetchall()]

        return {"finished": finished, "reading": reading, "wishlist": wishlist}
    finally:
        conn.close()




def build_user_context(discord_id: str, guild_id: str | None = None) -> dict:
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT user_id, taste_profile, reading_goal, goal_year FROM users WHERE discord_id = ?", (discord_id,))
        row = cur.fetchone()
        if not row:
            return {}
        user_id, taste_profile, reading_goal, goal_year = row
        query = """SELECT b.title, b.author, rp.status, rp.progress_raw, rp.progress_unit
                   FROM reading_progress rp
                   JOIN books b ON rp.book_id = b.book_id
                   WHERE rp.user_id = ?"""
        params = [user_id]
        if guild_id:
            query += " AND rp.guild_id = ?"
            params.append(guild_id)
        query += " ORDER BY rp.started_at DESC LIMIT 10"
        cur.execute(query, params)
        books = [
            {"title": r[0], "author": r[1], "status": r[2], "progress_raw": r[3], "progress_unit": r[4]}
            for r in cur.fetchall()
        ]
        context: dict[str, object] = {"books": books}
        if taste_profile:
            context["taste_profile"] = taste_profile
        # One connection for the whole build: this runs on every chat message.
        if reading_goal and goal_year:
            current_year = datetime.now(timezone.utc).year
            if goal_year == current_year:
                from actions_engagement import _goal_progress_count
                progress = _goal_progress_count(cur, user_id, guild_id, current_year) if guild_id else 0
                context["reading_goal"] = {"goal": reading_goal, "progress": progress, "year": current_year}
        if guild_id:
            from actions_engagement import _user_streaks
            streak = _user_streaks(cur, user_id, guild_id)
            if streak["current"] > 0:
                context["streak"] = f"You're on a {streak['current']}-day reading streak (longest: {streak['longest']})."
            cur.execute(
                "SELECT mode FROM user_tone_overrides WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id)
            )
            tone_row = cur.fetchone()
            if tone_row:
                context["tone_override"] = tone_row[0]
            from actions_session import _fetch_active
            club_read = _fetch_active(cur, guild_id)
            if club_read:
                context["club_read"] = {
                    "title": club_read.get("title"),
                    "author": club_read.get("author"),
                    "target_date": club_read.get("target_date"),
                    "started_at": club_read.get("started_at"),
                }
        return context
    finally:
        conn.close()
