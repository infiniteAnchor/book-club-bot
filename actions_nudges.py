"""Inactivity nudges and scheduled reminders."""
from datetime import datetime, timezone, timedelta

from database import connect
from actions import get_or_create_user, _parse_db_datetime
from actions_participation import DM_NUDGES_FEATURE, TRACKING_FEATURE


def get_inactivity_nudge_groups(guild_id: str, min_days: int, cooldown_days: int) -> list:
    """
    Returns grouped stale reading items that are eligible for an inactivity nudge.
    """
    now = datetime.now(timezone.utc)
    inactive_threshold = timedelta(days=min_days)
    cooldown_threshold = timedelta(days=cooldown_days)
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT rp.progress_id, u.user_id, u.discord_id, u.display_name,
                      b.title, b.author, rp.progress_raw, rp.progress_unit, rp.progress_pct,
                      rp.started_at, rp.last_activity_at, rp.last_nudged_at
               FROM reading_progress rp
               JOIN users u ON rp.user_id = u.user_id
               JOIN books b ON rp.book_id = b.book_id
               WHERE rp.guild_id = ? AND rp.status = 'reading'
                 AND EXISTS (
                    SELECT 1 FROM member_feature_preferences tracking_pref
                    WHERE tracking_pref.user_id = rp.user_id
                      AND tracking_pref.guild_id = rp.guild_id
                      AND tracking_pref.feature_key = ? AND tracking_pref.enabled = 1
                 )
                 AND EXISTS (
                    SELECT 1 FROM member_feature_preferences dm_pref
                    WHERE dm_pref.user_id = rp.user_id
                      AND dm_pref.guild_id = rp.guild_id
                      AND dm_pref.feature_key = ? AND dm_pref.enabled = 1
                 )
               ORDER BY u.display_name ASC, rp.progress_id ASC""",
            (guild_id, TRACKING_FEATURE, DM_NUDGES_FEATURE)
        )
        groups = {}
        for row in cur.fetchall():
            progress_id = row[0]
            user_id = row[1]
            discord_id = row[2]
            display_name = row[3]
            title = row[4]
            author = row[5]
            progress_raw = row[6]
            progress_unit = row[7]
            progress_pct = row[8]
            started_at = _parse_db_datetime(row[9])
            last_activity_at = _parse_db_datetime(row[10]) or started_at
            last_nudged_at = _parse_db_datetime(row[11])

            if not last_activity_at:
                continue
            if now - last_activity_at < inactive_threshold:
                continue
            if last_nudged_at and now - last_nudged_at < cooldown_threshold:
                continue

            group = groups.setdefault(
                user_id,
                {
                    "user_id": user_id,
                    "discord_id": discord_id,
                    "display_name": display_name,
                    "books": [],
                    "progress_ids": [],
                },
            )
            group["books"].append(
                {
                    "progress_id": progress_id,
                    "title": title,
                    "author": author,
                    "progress_raw": progress_raw,
                    "progress_unit": progress_unit,
                    "progress_pct": progress_pct,
                    "last_activity_at": last_activity_at.isoformat(),
                }
            )
            group["progress_ids"].append(progress_id)

        return list(groups.values())
    finally:
        conn.close()


def mark_progress_nudged(progress_ids: list[int]) -> None:
    if not progress_ids:
        return
    conn = connect()
    try:
        cur = conn.cursor()
        now = datetime.now(timezone.utc).isoformat()
        placeholders = ",".join("?" for _ in progress_ids)
        cur.execute(
            f"UPDATE reading_progress SET last_nudged_at = ? WHERE progress_id IN ({placeholders})",
            [now, *progress_ids]
        )
        conn.commit()
    finally:
        conn.close()


async def deliver_inactivity_nudge(send, group: dict, *args, **kwargs) -> bool:
    """Send one nudge and mark it only when Discord accepts the message.

    Forwards *args/**kwargs to `send` so prod can pass embed=... while tests
    still call with a positional content string.
    """
    try:
        await send(*args, **kwargs)
    except Exception:
        return False
    mark_progress_nudged(group["progress_ids"])
    return True



def _parse_reminder_time(scheduled_time: str) -> str | None:
    """
    Parse a scheduled_time string into an ISO timestamp string.
    Accepts:
    - ISO format timestamps (e.g. '2026-05-17T14:00:00')
    - Relative: 'in X hours/minutes/days'
    - 'tomorrow at HH:MM' or 'tomorrow at HH:MM am/pm'
    - 'today at HH:MM'
    Returns ISO format string, or None if unparseable.
    """
    if not scheduled_time:
        return None

    now = datetime.now(timezone.utc)
    text = scheduled_time.strip().lower()

    # Try ISO format first
    try:
        iso_text = text
        # Handle 'Z' suffix (Python < 3.11 doesn't support it)
        if iso_text.endswith('z'):
            iso_text = iso_text[:-1] + '+00:00'
        dt = datetime.fromisoformat(iso_text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt > now:
            return dt.isoformat()
    except (TypeError, ValueError):
        pass

    # Try 'in X hours/minutes/days'
    import re
    m = re.match(r'^in\s+(\d+)\s*(hour|hours|minute|minutes|day|days|min|mins|h)\s*$', text)
    if m:
        amount = int(m.group(1))
        unit = m.group(2)
        if unit in ('hour', 'hours', 'h'):
            dt = now + timedelta(hours=amount)
        elif unit in ('minute', 'minutes', 'min', 'mins'):
            dt = now + timedelta(minutes=amount)
        elif unit in ('day', 'days'):
            dt = now + timedelta(days=amount)
        else:
            return None
        return dt.isoformat()

    # Try 'tomorrow at HH:MM' or 'today at HH:MM'
    # Also handles 'tomorrow at 3pm' (without minutes), 'tomorrow at 3:00 pm', etc.
    m = re.match(r'^(tomorrow|today)\s+at\s+(\d{1,2})(?::(\d{2}))?(?:\s*(am|pm))?$', text)
    if m:
        day_offset = 1 if m.group(1) == 'tomorrow' else 0
        hour = int(m.group(2))
        minute = int(m.group(3)) if m.group(3) else 0
        meridian = m.group(4)
        if meridian:
            meridian = meridian.lower()
            if meridian == 'pm' and hour < 12:
                hour += 12
            elif meridian == 'am' and hour == 12:
                hour = 0
        base = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        dt = base + timedelta(days=day_offset)
        if dt <= now:
            dt += timedelta(days=1)  # If already past, assume next occurrence
        return dt.isoformat()

    return None


def action_set_reminder(discord_id: str, display_name: str, guild_id: str, args: dict) -> str:
    """
    Creates a reminder entry in the events table.
    Returns a confirmation string, or an error message string.
    """
    message = args.get("message")
    if not message:
        return "I need a reminder message to set a reminder."

    book_title = args.get("book_title")
    recurrence = args.get("recurrence", "once")
    scheduled_time_str = args.get("scheduled_time")

    if not scheduled_time_str:
        return "I need a time for the reminder. Try something like 'in 2 hours', 'tomorrow at 3pm', or an ISO timestamp."

    # Validate recurrence
    if recurrence not in ("once", "daily", "weekly", "monthly"):
        recurrence = "once"

    # Parse the scheduled time
    parsed_time = _parse_reminder_time(scheduled_time_str)
    if not parsed_time:
        return (
            "I couldn't understand that time. Try formats like:\n"
            "- 'in 2 hours'\n"
            "- 'tomorrow at 3pm'\n"
            "- '2026-05-17T14:00:00'"
        )

    conn = connect()
    try:
        user_id = get_or_create_user(conn, discord_id, display_name)

        # Look up book_id if book_title was provided
        book_id = None
        if book_title:
            cur = conn.cursor()
            cur.execute(
                "SELECT book_id FROM books WHERE LOWER(title) = LOWER(?)",
                (book_title,)
            )
            row = cur.fetchone()
            if row:
                book_id = row[0]

        cur = conn.cursor()
        cur.execute(
            """INSERT INTO events
               (user_id, book_id, guild_id, message, scheduled_time, recurrence, type, active)
               VALUES (?, ?, ?, ?, ?, ?, 'reminder', 1)""",
            (user_id, book_id, guild_id, message, parsed_time, recurrence)
        )
        conn.commit()

        # Format the time nicely for the confirmation
        dt = datetime.fromisoformat(parsed_time)
        time_str = dt.strftime("%A, %B %d at %I:%M %p %Z")

        confirmation = f"Reminder set for {time_str}: {message}"
        if recurrence != "once":
            confirmation += f" (repeats {recurrence})"
        if book_title:
            confirmation += f" Related to: {book_title}"

        return confirmation
    finally:
        conn.close()


def list_due_reminders(now: datetime | None = None) -> list[dict]:
    """Return active reminder rows whose scheduled_time is due."""
    now = now or datetime.now(timezone.utc)
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT e.event_id, u.discord_id, u.display_name, e.guild_id,
                      e.message, e.scheduled_time, e.recurrence, b.title
               FROM events e
               JOIN users u ON u.user_id = e.user_id
               LEFT JOIN books b ON b.book_id = e.book_id
               WHERE e.type = 'reminder' AND e.active = 1"""
        )
        due = []
        for row in cur.fetchall():
            scheduled = _parse_db_datetime(row[5])
            if scheduled is None:
                continue
            if scheduled.tzinfo is None:
                scheduled = scheduled.replace(tzinfo=timezone.utc)
            if scheduled <= now:
                due.append({
                    "event_id": row[0],
                    "discord_id": row[1],
                    "display_name": row[2],
                    "guild_id": row[3],
                    "message": row[4],
                    "scheduled_time": scheduled,
                    "recurrence": row[6] or "once",
                    "book_title": row[7],
                })
        return due
    finally:
        conn.close()


def _next_reminder_time(scheduled: datetime | None, recurrence: str, now: datetime) -> datetime | None:
    if scheduled is None or recurrence not in ("daily", "weekly", "monthly"):
        return None
    current = scheduled if scheduled.tzinfo else scheduled.replace(tzinfo=timezone.utc)

    def bump(value: datetime) -> datetime:
        if recurrence == "daily":
            return value + timedelta(days=1)
        if recurrence == "weekly":
            return value + timedelta(weeks=1)
        month = value.month + 1
        year = value.year + (1 if month > 12 else 0)
        month = 1 if month > 12 else month
        return value.replace(year=year, month=month, day=min(value.day, 28))

    nxt = bump(current)
    while nxt <= now:
        nxt = bump(nxt)
    return nxt


def mark_reminder_delivered(event_id: int, recurrence: str, scheduled_time, now: datetime | None = None) -> None:
    """Deactivate a one-shot reminder or advance a repeating one past `now`."""
    now = now or datetime.now(timezone.utc)
    conn = connect()
    try:
        cur = conn.cursor()
        nxt = _next_reminder_time(
            scheduled_time if isinstance(scheduled_time, datetime) else _parse_db_datetime(scheduled_time),
            recurrence,
            now,
        )
        if nxt is None:
            cur.execute("UPDATE events SET active = 0 WHERE event_id = ?", (event_id,))
        else:
            cur.execute(
                "UPDATE events SET scheduled_time = ? WHERE event_id = ?",
                (nxt.isoformat(), event_id),
            )
        conn.commit()
    finally:
        conn.close()

