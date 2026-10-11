import json
from datetime import datetime, timezone, timedelta
from database import connect
from actions import log_event, get_or_create_user, get_or_create_book, _parse_db_datetime


def open_nomination_window(guild_id: str, opened_by: str, days: int = 7) -> int:
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT window_id FROM nomination_windows WHERE guild_id = ? AND status = 'open'",
            (guild_id,)
        )
        if cur.fetchone():
            return 0
        closes_at = datetime.now(timezone.utc) + timedelta(days=days)
        cur.execute(
            """INSERT INTO nomination_windows (guild_id, opened_by, closes_at)
               VALUES (?, ?, ?)""",
            (guild_id, opened_by, closes_at.isoformat())
        )
        conn.commit()
        log_event(opened_by, guild_id, "open_nomination_window", {"days": days, "closes_at": closes_at.isoformat()})
        return cur.lastrowid or 0
    finally:
        conn.close()


def get_active_window(guild_id: str):
    """
    Returns the active nomination window for a guild as a dict, or None.
    """
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT window_id, guild_id, opened_by, opens_at, closes_at, status, closed_manually
               FROM nomination_windows
               WHERE guild_id = ? AND status = 'open'
               ORDER BY window_id DESC LIMIT 1""",
            (guild_id,)
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "window_id": row[0],
            "guild_id": row[1],
            "opened_by": row[2],
            "opens_at": row[3],
            "closes_at": row[4],
            "status": row[5],
            "closed_manually": bool(row[6]),
        }
    finally:
        conn.close()


def submit_nomination(
    discord_id: str, display_name: str, guild_id: str, title: str, author: str | None, reason: str | None,
):
    """
    Submits a new nomination in the active window for the guild.
    Returns (True, "ok") on success.
    Returns (False, "no_window") if no window is open.
    Returns (False, "already_nominated") if the user has already nominated in the active window.
    Returns (False, "missing_title") if title is missing.
    """
    if not title:
        return (False, "missing_title")
    window = get_active_window(guild_id)
    if not window:
        return (False, "no_window")
    conn = connect()
    try:
        user_id = get_or_create_user(conn, discord_id, display_name)
        book_id = get_or_create_book(conn, title, author)
        cur = conn.cursor()
        cur.execute(
            """SELECT nomination_id FROM nominations
               WHERE window_id = ? AND nominated_by = ?""",
            (window["window_id"], user_id)
        )
        if cur.fetchone():
            return (False, "already_nominated")
        cur.execute(
            """INSERT INTO nominations
               (window_id, book_id, nominated_by, nomination_reason, status)
               VALUES (?, ?, ?, ?, 'active')""",
            (window["window_id"], book_id, user_id, reason)
        )
        conn.commit()
        log_event(discord_id, guild_id, "nominate", {"title": title, "author": author, "reason": reason})
        return (True, "ok")
    finally:
        conn.close()


def apply_nomination(
    discord_id: str,
    display_name: str,
    guild_id: str,
    title: str,
    author: str | None,
    reason: str | None,
    *,
    replace_if_present: bool = False,
) -> str:
    """Submit a nomination, optionally replacing the member's existing pick.

    Returns ``ok``, ``replaced``, ``no_window``, ``already_nominated``, or
    ``missing_title``.
    """
    success, msg = submit_nomination(discord_id, display_name, guild_id, title, author, reason)
    if success:
        return "ok"
    if msg == "already_nominated" and replace_if_present:
        if replace_nomination(discord_id, guild_id, title, author, reason):
            return "replaced"
    return msg


def _nomination_lookup_needed(bound: dict) -> bool:
    """Mirror the read-only gates before these actions can create a book."""
    return bool(bound.get("title") and get_active_window(bound.get("guild_id")))


submit_nomination.book_lookup_needed = _nomination_lookup_needed
apply_nomination.book_lookup_needed = _nomination_lookup_needed


def replace_nomination(
    discord_id: str, guild_id: str, title: str, author: str | None, reason: str | None,
) -> bool:
    """
    Replaces the user's existing nomination in the active window.
    Returns True if a row was updated, False otherwise.
    """
    if not title:
        return False
    window = get_active_window(guild_id)
    if not window:
        return False
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users WHERE discord_id = ?", (discord_id,))
        row = cur.fetchone()
        if not row:
            return False
        user_id = row[0]
        book_id = get_or_create_book(conn, title, author)
        cur.execute(
            """UPDATE nominations
               SET book_id = ?, nomination_reason = ?, nominated_at = ?
               WHERE window_id = ? AND nominated_by = ?""",
            (book_id, reason, datetime.now(timezone.utc), window["window_id"], user_id)
        )
        conn.commit()
        success = cur.rowcount > 0
        if success:
            log_event(discord_id, guild_id, "replace_nomination", {"title": title, "author": author, "reason": reason})
        return success
    finally:
        conn.close()


replace_nomination.book_lookup_needed = _nomination_lookup_needed


def _fetch_window_nominations(conn, window_id: int) -> list:
    cur = conn.cursor()
    cur.execute(
        """SELECT n.nomination_id, u.display_name, b.title, b.author, n.nomination_reason
           FROM nominations n
           JOIN users u ON n.nominated_by = u.user_id
           JOIN books b ON n.book_id = b.book_id
           WHERE n.window_id = ?
           ORDER BY n.nominated_at ASC""",
        (window_id,)
    )
    return [
        {"nomination_id": r[0], "display_name": r[1], "title": r[2], "author": r[3], "reason": r[4]}
        for r in cur.fetchall()
    ]


def close_nomination_window(guild_id: str, manual: bool = False) -> list:
    """
    Closes the active nomination window for the guild. Returns the list of nominations.
    """
    window = get_active_window(guild_id)
    if not window:
        return []
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE nomination_windows
               SET status = 'closed', closed_manually = ?
               WHERE window_id = ?""",
            (1 if manual else 0, window["window_id"])
        )
        noms = _fetch_window_nominations(conn, window["window_id"])
        conn.commit()
        log_event(None, guild_id, "close_nomination_window", {"manual": manual, "nomination_count": len(noms)})
        return noms
    finally:
        conn.close()


def get_nominations(guild_id: str) -> list:
    """
    Returns all nominations for the guild's active window.
    """
    window = get_active_window(guild_id)
    if not window:
        return []
    conn = connect()
    try:
        return _fetch_window_nominations(conn, window["window_id"])
    finally:
        conn.close()


def open_poll(guild_id: str, opened_by: str, days: int = 7, is_tiebreaker: bool = False):
    """
    Opens a poll for a guild. If is_tiebreaker=False, uses the most recent closed nomination window.
    If is_tiebreaker=True, uses the active window (which should contain tiebreaker nominations).
    Returns (poll_id, list of nominations) on success.
    Returns (0, []) if no valid window exists or if a poll is already open.
    """
    conn = connect()
    try:
        cur = conn.cursor()
        # Check if poll already open
        cur.execute(
            "SELECT poll_id FROM polls WHERE guild_id = ? AND status = 'open'",
            (guild_id,)
        )
        if cur.fetchone():
            return (0, [])

        # Find the window to use
        if is_tiebreaker:
            # For tiebreaker, use active window (which will be the tiebreaker window)
            cur.execute(
                "SELECT window_id FROM nomination_windows WHERE guild_id = ? AND status = 'open' ORDER BY window_id DESC LIMIT 1",
                (guild_id,)
            )
        else:
            # For regular poll, use most recent closed window
            cur.execute(
                "SELECT window_id FROM nomination_windows WHERE guild_id = ? AND status = 'closed' ORDER BY window_id DESC LIMIT 1",
                (guild_id,)
            )

        window_row = cur.fetchone()
        if not window_row:
            return (0, [])
        window_id = window_row[0]

        # Fetch nominations for this window
        noms = _fetch_window_nominations(conn, window_id)
        if not noms:
            return (0, [])

        closes_at = datetime.now(timezone.utc) + timedelta(days=days)
        cur.execute(
            """INSERT INTO polls (guild_id, window_id, opened_by, closes_at, is_tiebreaker)
               VALUES (?, ?, ?, ?, ?)""",
            (guild_id, window_id, opened_by, closes_at.isoformat(), 1 if is_tiebreaker else 0)
        )
        conn.commit()
        poll_id = cur.lastrowid
        log_event(opened_by, guild_id, "open_poll", {"days": days, "is_tiebreaker": is_tiebreaker, "nomination_count": len(noms)})
        return (poll_id, noms)
    finally:
        conn.close()


def open_tiebreaker_poll(guild_id: str, opened_by: str, tied_nomination_ids: list, days: int = 7):
    """
    Opens a tiebreaker poll containing only the tied nominations.
    Creates a fresh nomination window, copies the tied nominations into it,
    and then opens a tiebreaker poll on that window.
    Returns (poll_id, list of nominations) on success, (0, []) on failure.
    """
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT poll_id FROM polls WHERE guild_id = ? AND status = 'open'",
            (guild_id,)
        )
        if cur.fetchone():
            return (0, [])

        closes_at = datetime.now(timezone.utc) + timedelta(days=days)
        # Closed so get_active_window ignores the runoff; nominations are copies only.
        cur.execute(
            """INSERT INTO nomination_windows (guild_id, opened_by, closes_at, status)
               VALUES (?, ?, ?, 'closed')""",
            (guild_id, opened_by, closes_at.isoformat())
        )
        conn.commit()
        new_window_id = cur.lastrowid

        copied_count = 0
        for nom_id in tied_nomination_ids:
            cur.execute(
                "SELECT book_id, nominated_by, nomination_reason FROM nominations WHERE nomination_id = ?",
                (nom_id,)
            )
            row = cur.fetchone()
            if not row:
                continue
            book_id, nominated_by, reason = row
            cur.execute(
                """INSERT INTO nominations (window_id, book_id, nominated_by, nomination_reason, status)
                   VALUES (?, ?, ?, ?, 'active')""",
                (new_window_id, book_id, nominated_by, reason)
            )
            copied_count += 1

        conn.commit()

        if copied_count == 0:
            cur.execute("UPDATE nomination_windows SET status = 'closed', closed_manually = 1 WHERE window_id = ?", (new_window_id,))
            conn.commit()
            return (0, [])

        cur.execute(
            """INSERT INTO polls (guild_id, window_id, opened_by, closes_at, is_tiebreaker)
               VALUES (?, ?, ?, ?, 1)""",
            (guild_id, new_window_id, opened_by, closes_at.isoformat())
        )
        conn.commit()
        poll_id = cur.lastrowid or 0
        if new_window_id is None:
            return (0, [])

        noms = _fetch_window_nominations(conn, new_window_id)
        return (poll_id, noms)
    finally:
        conn.close()


def get_active_poll(guild_id: str):
    """
    Returns the active poll for a guild with its nominations, or None.
    """
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT poll_id, guild_id, window_id, opened_by, opens_at, closes_at, status, is_tiebreaker
               FROM polls
               WHERE guild_id = ? AND status = 'open'
               ORDER BY poll_id DESC LIMIT 1""",
            (guild_id,)
        )
        row = cur.fetchone()
        if not row:
            return None
        poll = {
            "poll_id": row[0],
            "guild_id": row[1],
            "window_id": row[2],
            "opened_by": row[3],
            "opens_at": row[4],
            "closes_at": row[5],
            "status": row[6],
            "is_tiebreaker": bool(row[7]),
        }
        poll["nominations"] = _fetch_window_nominations(conn, poll["window_id"])
        return poll
    finally:
        conn.close()


def cast_vote(discord_id: str, display_name: str, guild_id: str, nomination_id: int, poll_id: int | None = None) -> tuple[bool, str]:
    """
    Casts or updates a vote on a nomination. Returns (success, message).
    One vote per user per poll, updating is allowed.
    """
    conn = connect()
    try:
        cur = conn.cursor()
        user_id = get_or_create_user(conn, discord_id, display_name)

        # Get active poll
        if poll_id is None:
            cur.execute(
                "SELECT poll_id FROM polls WHERE guild_id = ? AND status = 'open' ORDER BY poll_id DESC LIMIT 1",
                (guild_id,)
            )
        else:
            cur.execute(
                "SELECT poll_id FROM polls WHERE guild_id = ? AND poll_id = ? AND status = 'open'",
                (guild_id, poll_id)
            )
        poll_row = cur.fetchone()
        if not poll_row:
            return (False, "no_poll")
        poll_id = poll_row[0]

        cur.execute(
            """SELECT 1
               FROM nominations n
               JOIN polls p ON p.window_id = n.window_id
               WHERE p.poll_id = ? AND n.nomination_id = ?""",
            (poll_id, nomination_id)
        )
        if not cur.fetchone():
            return (False, "invalid_nomination")

        # Check if user already voted in this poll
        cur.execute(
            "SELECT vote_id FROM votes WHERE poll_id = ? AND user_id = ?",
            (poll_id, user_id)
        )
        existing = cur.fetchone()

        if existing:
            # Update existing vote
            cur.execute(
                "UPDATE votes SET nomination_id = ?, voted_at = ? WHERE vote_id = ?",
                (nomination_id, datetime.now(timezone.utc), existing[0])
            )
            conn.commit()
            log_event(discord_id, guild_id, "cast_vote", {"nomination_id": nomination_id, "poll_id": poll_id, "action": "updated"})
            return (True, "updated")
        else:
            # Insert new vote
            cur.execute(
                """INSERT INTO votes (poll_id, user_id, nomination_id)
                   VALUES (?, ?, ?)""",
                (poll_id, user_id, nomination_id)
            )
            conn.commit()
            log_event(discord_id, guild_id, "cast_vote", {"nomination_id": nomination_id, "poll_id": poll_id, "action": "new"})
            return (True, "new")
    finally:
        conn.close()


def close_poll(guild_id: str, manual: bool = False) -> dict:
    """
    Closes the active poll for a guild. Returns a dict with:
    - winner: nomination dict or None
    - tied: bool
    - tied_nominations: list if tied, empty list if no
    - all_results: list of {nomination, vote_count} sorted descending by votes
    """
    conn = connect()
    try:
        cur = conn.cursor()

        # Get active poll
        cur.execute(
            "SELECT poll_id, window_id FROM polls WHERE guild_id = ? AND status = 'open' ORDER BY poll_id DESC LIMIT 1",
            (guild_id,)
        )
        poll_row = cur.fetchone()
        if not poll_row:
            return {
                "winner": None,
                "tied": False,
                "tied_nominations": [],
                "all_results": []
            }

        poll_id, window_id = poll_row

        cur.execute("SELECT is_tiebreaker FROM polls WHERE poll_id = ?", (poll_id,))
        is_tiebreaker_poll = bool((cur.fetchone() or (0,))[0])

        # Close the poll
        cur.execute(
            "UPDATE polls SET status = 'closed', closed_manually = ? WHERE poll_id = ?",
            (1 if manual else 0, poll_id)
        )

        # Get vote counts per nomination
        cur.execute(
            """SELECT n.nomination_id, n.book_id, b.title, b.author, n.nomination_reason, COUNT(v.vote_id) as vote_count
               FROM nominations n
               LEFT JOIN votes v ON n.nomination_id = v.nomination_id AND v.poll_id = ?
               JOIN books b ON n.book_id = b.book_id
               WHERE n.window_id = ?
               GROUP BY n.nomination_id, n.book_id, b.title, b.author, n.nomination_reason
               ORDER BY vote_count DESC, n.nomination_id ASC""",
            (poll_id, window_id)
        )

        results = []
        for row in cur.fetchall():
            nom = {
                "nomination_id": row[0],
                "book_id": row[1],
                "title": row[2],
                "author": row[3],
                "reason": row[4],
            }
            results.append((nom, row[5]))

        conn.commit()

        log_event(None, guild_id, "close_poll", {"manual": manual, "is_tiebreaker": is_tiebreaker_poll, "total_votes": sum(v for _, v in results) if results else 0})

        # Determine winner and ties
        if not results:
            return {
                "winner": None,
                "tied": False,
                "tied_nominations": [],
                "all_results": [],
                "is_tiebreaker": is_tiebreaker_poll,
            }

        max_votes = results[0][1]
        winners = [nom for nom, votes in results if votes == max_votes]

        all_results = [{"nomination": nom, "vote_count": votes} for nom, votes in results]

        if max_votes == 0:
            return {
                "winner": None,
                "tied": False,
                "tied_nominations": [],
                "all_results": all_results,
                "is_tiebreaker": is_tiebreaker_poll,
            }

        if len(winners) > 1:
            return {
                "winner": None,
                "tied": True,
                "tied_nominations": winners,
                "all_results": all_results,
                "is_tiebreaker": is_tiebreaker_poll,
            }
        else:
            return {
                "winner": winners[0],
                "tied": False,
                "tied_nominations": [],
                "all_results": all_results,
                "is_tiebreaker": is_tiebreaker_poll,
            }
    finally:
        conn.close()


def get_poll_results(guild_id: str) -> list:
    """
    Returns current vote counts per nomination for the active poll.
    List of {nomination, vote_count} sorted descending by votes.
    """
    conn = connect()
    try:
        cur = conn.cursor()

        # Get active poll
        cur.execute(
            "SELECT poll_id, window_id FROM polls WHERE guild_id = ? AND status = 'open'",
            (guild_id,)
        )
        poll_row = cur.fetchone()
        if not poll_row:
            return []

        poll_id, window_id = poll_row

        # Get vote counts
        cur.execute(
            """SELECT n.nomination_id, b.title, b.author, nom.nomination_reason, COUNT(v.vote_id) as vote_count
               FROM nominations n
               LEFT JOIN votes v ON n.nomination_id = v.nomination_id AND v.poll_id = ?
               JOIN books b ON n.book_id = b.book_id
               JOIN nominations nom ON n.nomination_id = nom.nomination_id
               WHERE n.window_id = ?
               GROUP BY n.nomination_id, b.title, b.author, nom.nomination_reason
               ORDER BY vote_count DESC, n.nomination_id ASC""",
            (poll_id, window_id)
        )

        return [
            {
                "nomination": {
                    "nomination_id": row[0],
                    "title": row[1],
                    "author": row[2],
                    "reason": row[3],
                },
                "vote_count": row[4]
            }
            for row in cur.fetchall()
        ]
    finally:
        conn.close()


def open_coordination_poll(guild_id: str, opened_by: str, question: str, options: list, days: int = 3):
    """
    Opens a coordination poll (pace/date/scheduling) with freeform options.
    Returns (poll_id, list of {option_id, label}) on success, (0, []) on failure.
    """
    if not question or not options or len(options) < 2:
        return (0, [])
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT poll_id FROM coordination_polls WHERE guild_id = ? AND status = 'open'",
            (guild_id,)
        )
        if cur.fetchone():
            return (0, [])
        closes_at = datetime.now(timezone.utc) + timedelta(days=days)
        cur.execute(
            """INSERT INTO coordination_polls (guild_id, question, opened_by, closes_at)
               VALUES (?, ?, ?, ?)""",
            (guild_id, question, opened_by, closes_at.isoformat())
        )
        conn.commit()
        poll_id = cur.lastrowid
        option_rows = []
        for i, label in enumerate(options):
            cur.execute(
                """INSERT INTO coordination_options (poll_id, label, display_order)
                   VALUES (?, ?, ?)""",
                (poll_id, label, i)
            )
            option_rows.append({"option_id": cur.lastrowid, "label": label})
        conn.commit()
        log_event(opened_by, guild_id, "open_coordination_poll", {
            "question": question, "days": days, "options": options,
            "closes_at": closes_at.isoformat(),
        })
        return (poll_id, option_rows)
    finally:
        conn.close()


def get_active_coordination_poll(guild_id: str):
    """Returns the active coordination poll with its options, or None."""
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT poll_id, question, opened_by, opens_at, closes_at, status
               FROM coordination_polls
               WHERE guild_id = ? AND status = 'open'
               ORDER BY poll_id DESC LIMIT 1""",
            (guild_id,)
        )
        row = cur.fetchone()
        if not row:
            return None
        poll = {
            "poll_id": row[0],
            "question": row[1],
            "opened_by": row[2],
            "opens_at": row[3],
            "closes_at": row[4],
            "status": row[5],
        }
        cur.execute(
            "SELECT option_id, label FROM coordination_options WHERE poll_id = ? ORDER BY display_order",
            (poll["poll_id"],)
        )
        poll["options"] = [{"option_id": r[0], "label": r[1]} for r in cur.fetchall()]
        return poll
    finally:
        conn.close()


def cast_coordination_vote(discord_id: str, display_name: str, guild_id: str, option_id: int, poll_id: int | None = None) -> tuple:
    """Casts or updates a coordination vote. Returns (success, "new"|"updated"|"no_poll")."""
    conn = connect()
    try:
        cur = conn.cursor()
        user_id = get_or_create_user(conn, discord_id, display_name)
        if poll_id is None:
            cur.execute(
                "SELECT poll_id FROM coordination_polls WHERE guild_id = ? AND status = 'open' ORDER BY poll_id DESC LIMIT 1",
                (guild_id,)
            )
        else:
            cur.execute(
                "SELECT poll_id FROM coordination_polls WHERE guild_id = ? AND poll_id = ? AND status = 'open'",
                (guild_id, poll_id)
            )
        poll_row = cur.fetchone()
        if not poll_row:
            return (False, "no_poll")
        poll_id = poll_row[0]

        cur.execute(
            "SELECT 1 FROM coordination_options WHERE poll_id = ? AND option_id = ?",
            (poll_id, option_id)
        )
        if not cur.fetchone():
            return (False, "invalid_option")
        cur.execute(
            "SELECT vote_id FROM coordination_votes WHERE poll_id = ? AND user_id = ?",
            (poll_id, user_id)
        )
        existing = cur.fetchone()
        if existing:
            cur.execute(
                "UPDATE coordination_votes SET option_id = ?, voted_at = ? WHERE vote_id = ?",
                (option_id, datetime.now(timezone.utc), existing[0])
            )
            conn.commit()
            log_event(discord_id, guild_id, "cast_coordination_vote", {"option_id": option_id, "poll_id": poll_id, "action": "updated"})
            return (True, "updated")
        else:
            cur.execute(
                "INSERT INTO coordination_votes (poll_id, user_id, option_id) VALUES (?, ?, ?)",
                (poll_id, user_id, option_id)
            )
            conn.commit()
            log_event(discord_id, guild_id, "cast_coordination_vote", {"option_id": option_id, "poll_id": poll_id, "action": "new"})
            return (True, "new")
    finally:
        conn.close()


def get_coordination_poll_results(guild_id: str) -> list:
    """Returns vote counts per option for the active coordination poll."""
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT poll_id FROM coordination_polls WHERE guild_id = ? AND status = 'open'",
            (guild_id,)
        )
        poll_row = cur.fetchone()
        if not poll_row:
            return []
        poll_id = poll_row[0]
        cur.execute(
            """SELECT co.option_id, co.label, COUNT(cv.vote_id) as vote_count
               FROM coordination_options co
               LEFT JOIN coordination_votes cv ON co.option_id = cv.option_id AND cv.poll_id = ?
               WHERE co.poll_id = ?
               GROUP BY co.option_id, co.label
               ORDER BY vote_count DESC, co.display_order ASC""",
            (poll_id, poll_id)
        )
        return [{"option_id": r[0], "label": r[1], "vote_count": r[2]} for r in cur.fetchall()]
    finally:
        conn.close()


def close_coordination_poll(guild_id: str, manual: bool = False) -> dict:
    """
    Closes the active coordination poll. Returns dict with winner, tied, all_results.
    """
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT poll_id FROM coordination_polls WHERE guild_id = ? AND status = 'open' ORDER BY poll_id DESC LIMIT 1",
            (guild_id,)
        )
        poll_row = cur.fetchone()
        if not poll_row:
            log_event(None, guild_id, "close_coordination_poll", {"manual": manual, "found": False})
            return {"winner": None, "tied": False, "tied_options": [], "all_results": []}
        poll_id = poll_row[0]
        cur.execute(
            "UPDATE coordination_polls SET status = 'closed', closed_manually = ? WHERE poll_id = ?",
            (1 if manual else 0, poll_id)
        )
        cur.execute(
            """SELECT co.option_id, co.label, COUNT(cv.vote_id) as vote_count
               FROM coordination_options co
               LEFT JOIN coordination_votes cv ON co.option_id = cv.option_id AND cv.poll_id = ?
               WHERE co.poll_id = ?
               GROUP BY co.option_id, co.label
               ORDER BY vote_count DESC, co.display_order ASC""",
            (poll_id, poll_id)
        )
        rows = cur.fetchall()
        conn.commit()
        if not rows:
            log_event(None, guild_id, "close_coordination_poll", {"manual": manual, "found": True, "no_votes": True})
            return {"winner": None, "tied": False, "tied_options": [], "all_results": []}
        all_results = [{"option_id": r[0], "label": r[1], "vote_count": r[2]} for r in rows]
        max_votes = rows[0][2]
        winners = [r for r in all_results if r["vote_count"] == max_votes]
        if len(winners) > 1:
            log_event(None, guild_id, "close_coordination_poll", {"manual": manual, "found": True, "tied": True, "winners": [w["label"] for w in winners]})
            return {"winner": None, "tied": True, "tied_options": winners, "all_results": all_results}
        log_event(None, guild_id, "close_coordination_poll", {"manual": manual, "found": True, "winner": winners[0]["label"], "vote_count": winners[0]["vote_count"]})
        return {"winner": winners[0], "tied": False, "tied_options": [], "all_results": all_results}
    finally:
        conn.close()
