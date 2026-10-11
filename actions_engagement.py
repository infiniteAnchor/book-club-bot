"""Social engagement: digests, achievements, goals, streaks, tone, admin summary."""
from datetime import date, datetime, timezone

from database import connect
from actions import log_event, get_or_create_user

# Imported Goodreads history fuels personal recs; club surfaces ignore it.
_CLUB_SOURCE = "(source IS NULL OR source != 'goodreads')"
_CLUB_PROGRESS = "(rp.source IS NULL OR rp.source != 'goodreads')"
_CLUB_RATING = "(r.source IS NULL OR r.source != 'goodreads')"


def get_nomination_context_for_book(discord_id: str, title: str, guild_id: str | None = None, author: str | None = None) -> dict:
    """
    Returns the most relevant nomination context for a book, if one exists.
    Prefers the user's own nomination when available.
    """
    conn = connect()
    try:
        cur = conn.cursor()
        query = """
            SELECT n.nomination_reason, n.nominated_at, u.display_name
            FROM nominations n
            JOIN books b ON n.book_id = b.book_id
            JOIN users u ON n.nominated_by = u.user_id
            WHERE LOWER(b.title) = LOWER(?)
        """
        params = [title]
        if author:
            query += " AND LOWER(COALESCE(b.author, '')) = LOWER(?)"
            params.append(author)
        if guild_id:
            query += """
              AND (
                    n.window_id IN (
                        SELECT window_id
                        FROM nomination_windows
                        WHERE guild_id = ?
                    )
                    OR n.nominated_by = (
                        SELECT user_id FROM users WHERE discord_id = ?
                    )
                  )
            """
            params.extend([guild_id, discord_id])
        query += " ORDER BY CASE WHEN u.discord_id = ? THEN 0 ELSE 1 END, n.nominated_at DESC LIMIT 1"
        params.append(discord_id)
        cur.execute(query, params)
        row = cur.fetchone()
        if not row:
            return {}
        return {
            "nomination_reason": row[0],
            "nominated_at": row[1],
            "nominated_by": row[2],
        }
    finally:
        conn.close()


def get_weekly_activity(guild_id: str) -> dict:
    """
    Returns a summary of reading activity in the last 7 days for all users in a guild.
    """
    conn = connect()
    try:
        cur = conn.cursor()
        # Books started in last 7 days
        cur.execute(
            """SELECT u.display_name, b.title, b.author
               FROM reading_progress rp
               JOIN users u ON rp.user_id = u.user_id
               JOIN books b ON rp.book_id = b.book_id
               WHERE rp.guild_id = ? AND rp.status = 'reading'
               AND rp.started_at >= datetime('now', '-7 days')""",
            (guild_id,)
        )
        started = [{"user": r[0], "title": r[1], "author": r[2]} for r in cur.fetchall()]
        # Books finished in last 7 days
        cur.execute(
            f"""SELECT u.display_name, b.title, b.author
               FROM reading_progress rp
               JOIN users u ON rp.user_id = u.user_id
               JOIN books b ON rp.book_id = b.book_id
               WHERE rp.guild_id = ? AND rp.status = 'finished'
               AND {_CLUB_PROGRESS}
               AND rp.finished_at >= datetime('now', '-7 days')""",
            (guild_id,)
        )
        finished = [{"user": r[0], "title": r[1], "author": r[2]} for r in cur.fetchall()]
        # Ratings logged in last 7 days
        cur.execute(
            f"""SELECT u.display_name, b.title, r.score, r.review_notes
               FROM ratings r
               JOIN users u ON r.user_id = u.user_id
               JOIN books b ON r.book_id = b.book_id
               WHERE r.rated_at >= datetime('now', '-7 days')
               AND {_CLUB_RATING}
               AND EXISTS (
                   SELECT 1
                   FROM reading_progress rp
                   WHERE rp.user_id = r.user_id
                   AND rp.book_id = r.book_id
                   AND rp.guild_id = ?
                   AND {_CLUB_PROGRESS}
               )""",
            (guild_id,)
        )
        ratings = [{"user": r[0], "title": r[1], "score": r[2], "note": r[3]} for r in cur.fetchall()]
        # Currently reading (for context)
        cur.execute(
            """SELECT u.display_name, b.title, b.author, rp.progress_raw, rp.progress_unit
               FROM reading_progress rp
               JOIN users u ON rp.user_id = u.user_id
               JOIN books b ON rp.book_id = b.book_id
               WHERE rp.guild_id = ? AND rp.status = 'reading'""",
            (guild_id,)
        )
        currently_reading = [{"user": r[0], "title": r[1], "author": r[2], "progress_raw": r[3], "progress_unit": r[4]} for r in cur.fetchall()]
        return {
            "started": started,
            "finished": finished,
            "ratings": ratings,
            "currently_reading": currently_reading
        }
    finally:
        conn.close()


_ACHIEVEMENTS = [
    ("first_finish",   "Page Turner",     "Finished your first book in the club!"),
    ("five_books",     "Bookworm",        "5 books finished. You're officially a reader."),
    ("ten_books",      "Voracious",       "10 books down. The library trembles."),
    ("twenty_books",   "Literary Beast",  "20 books finished. Truly unstoppable."),
    ("first_rating",   "Critic",          "Logged your first rating. The discourse begins."),
    ("harsh_critic",   "Harsh Critic",    "Gave a score of 3 or lower. Standards maintained."),
    ("generous_soul",  "Generous Soul",   "Gave a perfect 10. True love found."),
    ("dnf_survivor",   "Life's Too Short","DNF'd your first book. No shame — time is precious."),
    ("speed_reader",   "Speed Demon",     "Finished a book within 3 days of starting it."),
    ("streak_7",       "Consistent",      "Maintained a 7-day reading streak."),
    ("streak_30",      "Dedicated",       "Maintained a 30-day reading streak."),
    ("streak_100",     "Unstoppable",     "Maintained a 100-day reading streak. Legendary."),
]


def check_achievements(discord_id: str, display_name: str, guild_id: str) -> list:
    """
    Checks all achievement conditions for a user and awards any newly earned ones.
    Returns a list of newly earned achievement dicts.
    """
    conn = connect()
    try:
        user_id = get_or_create_user(conn, discord_id, display_name)
        cur = conn.cursor()

        cur.execute("SELECT achievement_type FROM achievements WHERE user_id = ? AND guild_id = ?", (user_id, guild_id))
        already_earned = {row[0] for row in cur.fetchall()}

        cur.execute(
            f"""SELECT COUNT(*) FROM reading_progress
               WHERE user_id = ? AND guild_id = ? AND status = 'finished'
               AND {_CLUB_SOURCE}""",
            (user_id, guild_id)
        )
        finished_count = cur.fetchone()[0]

        cur.execute(
            f"""SELECT COUNT(*)
               FROM ratings r
               WHERE r.user_id = ?
               AND {_CLUB_RATING}
               AND EXISTS (
                   SELECT 1
                   FROM reading_progress rp
                   WHERE rp.user_id = r.user_id
                   AND rp.book_id = r.book_id
                   AND rp.guild_id = ?
                   AND {_CLUB_PROGRESS}
               )""",
            (user_id, guild_id)
        )
        rated_count = cur.fetchone()[0]

        cur.execute(
            f"""SELECT COUNT(*) FROM reading_progress
               WHERE user_id = ? AND guild_id = ? AND status = 'dnf'
               AND {_CLUB_SOURCE}""",
            (user_id, guild_id)
        )
        dnf_count = cur.fetchone()[0]

        cur.execute(
            f"""SELECT COUNT(*)
               FROM ratings r
               WHERE r.user_id = ? AND r.score <= 3
               AND {_CLUB_RATING}
               AND EXISTS (
                   SELECT 1
                   FROM reading_progress rp
                   WHERE rp.user_id = r.user_id
                   AND rp.book_id = r.book_id
                   AND rp.guild_id = ?
                   AND {_CLUB_PROGRESS}
               )""",
            (user_id, guild_id)
        )
        low_ratings = cur.fetchone()[0]

        cur.execute(
            f"""SELECT COUNT(*)
               FROM ratings r
               WHERE r.user_id = ? AND r.score = 10
               AND {_CLUB_RATING}
               AND EXISTS (
                   SELECT 1
                   FROM reading_progress rp
                   WHERE rp.user_id = r.user_id
                   AND rp.book_id = r.book_id
                   AND rp.guild_id = ?
                   AND {_CLUB_PROGRESS}
               )""",
            (user_id, guild_id)
        )
        perfect_ratings = cur.fetchone()[0]

        cur.execute(
            f"""SELECT COUNT(*) FROM reading_progress
               WHERE user_id = ? AND guild_id = ? AND status = 'finished'
               AND {_CLUB_SOURCE}
               AND started_at IS NOT NULL AND finished_at IS NOT NULL
               AND julianday(finished_at) - julianday(started_at) <= 3""",
            (user_id, guild_id)
        )
        fast_finish = cur.fetchone()[0] > 0

        current_streak = _user_streaks(cur, user_id, guild_id)["current"]

        conditions = {
            "first_finish":  finished_count >= 1,
            "five_books":    finished_count >= 5,
            "ten_books":     finished_count >= 10,
            "twenty_books":  finished_count >= 20,
            "first_rating":  rated_count >= 1,
            "harsh_critic":  low_ratings >= 1,
            "generous_soul": perfect_ratings >= 1,
            "dnf_survivor":  dnf_count >= 1,
            "speed_reader":  fast_finish,
            "streak_7":      current_streak >= 7,
            "streak_30":     current_streak >= 30,
            "streak_100":    current_streak >= 100,
        }

        now = datetime.now(timezone.utc)
        newly_earned = []
        for ach_type, title, description in _ACHIEVEMENTS:
            if ach_type in already_earned:
                continue
            if conditions.get(ach_type):
                cur.execute(
                    """INSERT INTO achievements (user_id, guild_id, achievement_type, title, description, earned_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (user_id, guild_id, ach_type, title, description, now)
                )
                newly_earned.append({"type": ach_type, "title": title, "description": description})

        if newly_earned:
            conn.commit()
        return newly_earned
    finally:
        conn.close()


def get_year_activity(guild_id: str) -> dict:
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""SELECT u.display_name, b.title, b.author
               FROM reading_progress rp
               JOIN users u ON rp.user_id = u.user_id
               JOIN books b ON rp.book_id = b.book_id
               WHERE rp.guild_id = ? AND rp.status = 'finished'
               AND {_CLUB_PROGRESS}
               AND rp.finished_at >= datetime('now', '-365 days')""",
            (guild_id,)
        )
        finished = [{"user": r[0], "title": r[1], "author": r[2]} for r in cur.fetchall()]

        cur.execute(
            f"""SELECT u.display_name, b.title, b.author
               FROM reading_progress rp
               JOIN users u ON rp.user_id = u.user_id
               JOIN books b ON rp.book_id = b.book_id
               WHERE rp.guild_id = ? AND rp.status = 'dnf'
               AND {_CLUB_PROGRESS}
               AND rp.finished_at >= datetime('now', '-365 days')""",
            (guild_id,)
        )
        dnf = [{"user": r[0], "title": r[1], "author": r[2]} for r in cur.fetchall()]

        cur.execute(
            f"""SELECT u.display_name, b.title, r.score
               FROM ratings r
               JOIN users u ON r.user_id = u.user_id
               JOIN books b ON r.book_id = b.book_id
               WHERE r.rated_at >= datetime('now', '-365 days')
               AND {_CLUB_RATING}
               AND EXISTS (
                   SELECT 1
                   FROM reading_progress rp
                   WHERE rp.user_id = r.user_id
                   AND rp.book_id = r.book_id
                   AND rp.guild_id = ?
                   AND {_CLUB_PROGRESS}
               )""",
            (guild_id,)
        )
        ratings = [{"user": r[0], "title": r[1], "score": r[2]} for r in cur.fetchall()]

        cur.execute(
            """SELECT u.display_name, b.title, b.author
               FROM reading_progress rp
               JOIN users u ON rp.user_id = u.user_id
               JOIN books b ON rp.book_id = b.book_id
               WHERE rp.guild_id = ? AND rp.status = 'reading'""",
            (guild_id,)
        )
        currently_reading = [{"user": r[0], "title": r[1], "author": r[2]} for r in cur.fetchall()]

        cur.execute(
            """SELECT u.display_name, a.title
               FROM achievements a
               JOIN users u ON a.user_id = u.user_id
               WHERE a.guild_id = ?
               AND a.earned_at >= datetime('now', '-365 days')""",
            (guild_id,)
        )
        achievements = [{"user": r[0], "achievement": r[1]} for r in cur.fetchall()]

        return {
            "finished": finished,
            "dnf": dnf,
            "ratings": ratings,
            "currently_reading": currently_reading,
            "achievements": achievements,
        }
    finally:
        conn.close()


def update_user_taste_profile(discord_id: str, profile_text: str) -> None:
    conn = connect()
    try:
        conn.execute(
            "UPDATE users SET taste_profile = ? WHERE discord_id = ?",
            (profile_text, discord_id)
        )
        conn.commit()
    finally:
        conn.close()


def get_all_active_users(guild_id: str) -> list:
    """Returns all users with at least one finished book in the guild."""
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT DISTINCT u.discord_id, u.display_name
               FROM users u
               JOIN reading_progress rp ON rp.user_id = u.user_id
               WHERE rp.guild_id = ? AND rp.status = 'finished'""",
            (guild_id,)
        )
        return [{"discord_id": r[0], "display_name": r[1]} for r in cur.fetchall()]
    finally:
        conn.close()


def action_set_goal(discord_id: str, display_name: str, args: dict) -> bool:
    goal = args.get("goal")
    if not goal:
        return False
    try:
        goal = int(goal)
    except (TypeError, ValueError):
        return False
    if goal < 1 or goal > 365:
        return False

    conn = connect()
    try:
        user_id = get_or_create_user(conn, discord_id, display_name)
        year = datetime.now(timezone.utc).year
        conn.execute(
            "UPDATE users SET reading_goal = ?, goal_year = ? WHERE user_id = ?",
            (goal, year, user_id)
        )
        conn.commit()
        log_event(discord_id, None, "set_goal", {"goal": goal, "year": year})
        return True
    finally:
        conn.close()


def get_goal_progress(discord_id: str, guild_id: str) -> dict:
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT user_id, reading_goal, goal_year FROM users WHERE discord_id = ?", (discord_id,))
        row = cur.fetchone()
        if not row or not row[1]:
            return {}
        user_id, goal, goal_year = row
        current_year = datetime.now(timezone.utc).year
        if goal_year != current_year:
            return {}
        progress = _goal_progress_count(cur, user_id, guild_id, current_year)
        return {"goal": goal, "progress": progress, "year": current_year}
    finally:
        conn.close()


def _goal_progress_count(cur, user_id: int, guild_id: str, year: int) -> int:
    """Club books this user finished in `year` in this guild (imports excluded)."""
    cur.execute(
        f"""SELECT COUNT(*) FROM reading_progress
           WHERE user_id = ? AND guild_id = ? AND status = 'finished'
           AND {_CLUB_SOURCE}
           AND strftime('%Y', finished_at) = ?""",
        (user_id, guild_id, str(year))
    )
    return cur.fetchone()[0]


def get_leaderboard_stats(guild_id: str) -> list:
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""SELECT u.display_name,
                      COUNT(CASE WHEN rp.status = 'finished' THEN 1 END) AS books_finished,
                      COALESCE(SUM(CASE WHEN rp.status = 'finished' THEN b.page_count ELSE 0 END), 0) AS pages_read,
                      COUNT(CASE WHEN rp.status = 'dnf' THEN 1 END) AS dnf_count
               FROM users u
               LEFT JOIN reading_progress rp
                 ON rp.user_id = u.user_id AND rp.guild_id = ?
                 AND {_CLUB_PROGRESS}
               LEFT JOIN books b ON b.book_id = rp.book_id
               GROUP BY u.user_id, u.display_name
               HAVING books_finished > 0 OR dnf_count > 0
               ORDER BY books_finished DESC, pages_read DESC""",
            (guild_id,)
        )
        rows = cur.fetchall()

        cur.execute(
            f"""SELECT u.display_name,
                      COUNT(r.rating_id) AS ratings_given,
                      ROUND(AVG(r.score), 1) AS avg_score
               FROM users u
               LEFT JOIN ratings r ON r.user_id = u.user_id
               AND {_CLUB_RATING}
               AND EXISTS (
                   SELECT 1
                   FROM reading_progress rp
                   WHERE rp.user_id = r.user_id
                   AND rp.book_id = r.book_id
                   AND rp.guild_id = ?
                   AND {_CLUB_PROGRESS}
               )
               GROUP BY u.user_id, u.display_name
               HAVING ratings_given > 0""",
            (guild_id,)
        )
        rating_rows = {r[0]: {"ratings_given": r[1], "avg_score": r[2]} for r in cur.fetchall()}

        stats = []
        for row in rows:
            name = row[0]
            entry = {
                "name": name,
                "books_finished": row[1],
                "pages_read": row[2],
                "dnf_count": row[3],
            }
            if name in rating_rows:
                entry.update(rating_rows[name])
            stats.append(entry)

        return stats
    finally:
        conn.close()

def get_club_history(guild_id: str, limit: int = 50) -> list:
    """Returns finished club books, recent-first.

    Each row: {book_id, title, author, finishes, finishers, avg_score,
    ratings_count, last_finished_at}. Guild-scoped; DNF excluded.
    """
    try:
        count = int(limit)
    except (TypeError, ValueError):
        count = 50
    count = max(1, min(count, 50))
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT b.book_id, b.title, b.author,
                      COUNT(*) AS finishes,
                      MAX(rp.finished_at) AS last_finished_at,
                      GROUP_CONCAT(DISTINCT u.display_name) AS finishers
               FROM reading_progress rp
               JOIN books b ON b.book_id = rp.book_id
               JOIN users u ON u.user_id = rp.user_id
               WHERE rp.guild_id = ? AND rp.status = 'finished'
               AND (rp.source IS NULL OR rp.source != 'goodreads')
               GROUP BY b.book_id, b.title, b.author
               ORDER BY last_finished_at DESC
               LIMIT ?""",
            (guild_id, count)
        )
        rows = cur.fetchall()
        cur.execute(
            f"""SELECT r.book_id,
                      ROUND(AVG(r.score), 1) AS avg_score,
                      COUNT(r.rating_id) AS ratings_count
               FROM ratings r
               WHERE {_CLUB_RATING}
               AND EXISTS (
                   SELECT 1
                   FROM reading_progress rp
                   WHERE rp.user_id = r.user_id
                   AND rp.book_id = r.book_id
                   AND rp.guild_id = ?
                   AND rp.status = 'finished'
                   AND {_CLUB_PROGRESS}
               )
               GROUP BY r.book_id""",
            (guild_id,)
        )
        rating_rows = {r[0]: {"avg_score": r[1], "ratings_count": r[2]} for r in cur.fetchall()}
        history = []
        for row in rows:
            entry = {
                "book_id": row[0],
                "title": row[1],
                "author": row[2],
                "finishes": row[3],
                "last_finished_at": row[4],
                "finishers": row[5] or "",
            }
            if row[0] in rating_rows:
                entry.update(rating_rows[row[0]])
            history.append(entry)
        return history
    finally:
        conn.close()


def get_admin_summary(guild_id: str) -> dict:
    conn = connect()
    try:
        cur = conn.cursor()

        cur.execute(
            """SELECT u.display_name, b.title, rp.progress_raw, rp.progress_unit
               FROM reading_progress rp
               JOIN users u ON rp.user_id = u.user_id
               JOIN books b ON rp.book_id = b.book_id
               WHERE rp.guild_id = ? AND rp.status = 'reading'""",
            (guild_id,)
        )
        currently_reading = [{"user": r[0], "title": r[1], "progress": r[2], "unit": r[3]} for r in cur.fetchall()]

        cur.execute(
            "SELECT COUNT(DISTINCT user_id) FROM reading_progress WHERE guild_id = ?",
            (guild_id,)
        )
        total_users = cur.fetchone()[0]

        cur.execute(
            f"""SELECT COUNT(*) FROM reading_progress
               WHERE guild_id = ? AND status = 'finished'
               AND {_CLUB_SOURCE}""",
            (guild_id,)
        )
        total_finished = cur.fetchone()[0]

        cur.execute(
            f"""SELECT u.display_name, b.title, r.score
               FROM ratings r
               JOIN users u ON r.user_id = u.user_id
               JOIN books b ON r.book_id = b.book_id
               WHERE r.rated_at >= datetime('now', '-7 days')
               AND {_CLUB_RATING}
               AND EXISTS (
                   SELECT 1
                   FROM reading_progress rp
                   WHERE rp.user_id = r.user_id
                   AND rp.book_id = r.book_id
                   AND rp.guild_id = ?
                   AND {_CLUB_PROGRESS}
               )""",
            (guild_id,)
        )
        recent_ratings = [{"user": r[0], "title": r[1], "score": r[2]} for r in cur.fetchall()]

        cur.execute(
            """SELECT window_id, closes_at, status FROM nomination_windows
               WHERE guild_id = ? AND status = 'open'""",
            (guild_id,)
        )
        row = cur.fetchone()
        active_window = {"window_id": row[0], "closes_at": row[1]} if row else None

        cur.execute(
            "SELECT poll_id, closes_at, is_tiebreaker FROM polls WHERE guild_id = ? AND status = 'open'",
            (guild_id,)
        )
        row = cur.fetchone()
        active_poll = {"poll_id": row[0], "closes_at": row[1], "is_tiebreaker": bool(row[2])} if row else None

        cur.execute(
            "SELECT poll_id, question, closes_at FROM coordination_polls WHERE guild_id = ? AND status = 'open'",
            (guild_id,)
        )
        row = cur.fetchone()
        active_coord_poll = {"poll_id": row[0], "question": row[1], "closes_at": row[2]} if row else None

        cur.execute(
            """SELECT u.display_name, a.title
               FROM achievements a
               JOIN users u ON a.user_id = u.user_id
               WHERE a.guild_id = ?
               AND a.earned_at >= datetime('now', '-7 days')""",
            (guild_id,)
        )
        recent_achievements = [{"user": r[0], "achievement": r[1]} for r in cur.fetchall()]

        return {
            "total_users": total_users,
            "total_finished": total_finished,
            "currently_reading": currently_reading,
            "recent_ratings": recent_ratings,
            "active_nomination_window": active_window,
            "active_poll": active_poll,
            "active_coordination_poll": active_coord_poll,
            "recent_achievements": recent_achievements,
        }
    finally:
        conn.close()


def _streaks_from_dates(dates: list, today) -> dict:
    """Current and longest runs of consecutive days; `dates` is distinct, newest first.

    A streak stays current through today, so a reader who logged yesterday but
    not yet today keeps their full count.
    """
    if not dates:
        return {"current": 0, "longest": 0}
    runs = [1]
    for newer, older in zip(dates, dates[1:]):
        if (newer - older).days == 1:
            runs[-1] += 1
        else:
            runs.append(1)
    current = runs[0] if (today - dates[0]).days <= 1 else 0
    return {"current": current, "longest": max(runs)}


def _user_streaks(cur, user_id: int, guild_id: str) -> dict:
    cur.execute(
        """SELECT activity_date FROM reading_activity
           WHERE user_id = ? AND guild_id = ?
           ORDER BY activity_date DESC""",
        (user_id, guild_id)
    )
    dates = [date.fromisoformat(row[0]) for row in cur.fetchall()]
    # Activity days are recorded in UTC, regardless of host timezone.
    return _streaks_from_dates(dates, datetime.now(timezone.utc).date())


def get_streaks(discord_id: str, guild_id: str) -> dict:
    """Returns {current: int, longest: int} reading streak for a user."""
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users WHERE discord_id = ?", (discord_id,))
        row = cur.fetchone()
        if not row:
            return {"current": 0, "longest": 0}
        return _user_streaks(cur, row[0], guild_id)
    finally:
        conn.close()


def get_streak_leaderboard(guild_id: str) -> list:
    """Returns top 10 users by current streak."""
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT DISTINCT u.user_id, u.display_name
               FROM reading_activity ra
               JOIN users u ON ra.user_id = u.user_id
               WHERE ra.guild_id = ?""",
            (guild_id,)
        )
        streaks = []
        for user_id, display_name in cur.fetchall():
            s = _user_streaks(cur, user_id, guild_id)
            if s["current"] > 0:
                streaks.append({"display_name": display_name, "current": s["current"], "longest": s["longest"]})
        streaks.sort(key=lambda x: x["current"], reverse=True)
        return streaks[:10]
    finally:
        conn.close()


def set_user_tone_override(target_discord_id: str, target_display_name: str, guild_id: str | None, mode: str, updated_by: str) -> str:
    """Sets a user's tone override. Returns a confirmation string."""
    if mode not in ("glaze", "roast", "neutral"):
        return f"Invalid mode '{mode}'. Use 'glaze', 'roast', or 'neutral'."
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users WHERE discord_id = ?", (target_discord_id,))
        row = cur.fetchone()
        if not row:
            return f"User not found."
        user_id = row[0]
        if mode == "neutral":
            cur.execute(
                "DELETE FROM user_tone_overrides WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id)
            )
            conn.commit()
            return f"Cleared tone override for **{target_display_name}**."
        cur.execute(
            """INSERT INTO user_tone_overrides (guild_id, user_id, mode, updated_by)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(guild_id, user_id) DO UPDATE SET mode = excluded.mode, updated_by = excluded.updated_by, updated_at = CURRENT_TIMESTAMP""",
            (guild_id, user_id, mode, updated_by)
        )
        conn.commit()
        return f"Set **{target_display_name}**'s tone to **{mode}**."
    finally:
        conn.close()


def clear_user_tone_override(target_discord_id: str, guild_id: str | None) -> str:
    """Removes a user's tone override. Returns a confirmation string."""
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users WHERE discord_id = ?", (target_discord_id,))
        row = cur.fetchone()
        if not row:
            return "User not found."
        user_id = row[0]
        cur.execute(
            "DELETE FROM user_tone_overrides WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id)
        )
        conn.commit()
        if cur.rowcount > 0:
            return "Tone override cleared."
        return "No tone override was set."
    finally:
        conn.close()


def list_user_tone_overrides(guild_id: str | None) -> list:
    """Returns all active tone overrides in a guild."""
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT u.display_name, uto.mode, uto.updated_by
               FROM user_tone_overrides uto
               JOIN users u ON uto.user_id = u.user_id
               WHERE uto.guild_id = ?""",
            (guild_id,)
        )
        return [{"display_name": r[0], "mode": r[1], "set_by": r[2]} for r in cur.fetchall()]
    finally:
        conn.close()

