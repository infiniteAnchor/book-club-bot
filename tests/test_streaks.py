"""Reading streaks: UTC activity days recorded per member and guild (BCB-010)."""
import datetime as dt

import pytest

import actions
import actions_engagement
import database
from actions import action_start_book, action_update_progress


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "bookclub.db"))
    database.init_db()


def _freeze(monkeypatch, module, utc_now):
    class FixedDatetime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return utc_now if tz is None else utc_now.astimezone(tz)

    monkeypatch.setattr(module, "datetime", FixedDatetime)


def _activity_dates(discord_id="alice", guild_id="guild-1"):
    conn = database.connect()
    try:
        return [
            row[0] for row in conn.execute(
                """SELECT activity_date FROM reading_activity ra
                   JOIN users u ON ra.user_id = u.user_id
                   WHERE u.discord_id = ? AND ra.guild_id = ?
                   ORDER BY activity_date""",
                (discord_id, guild_id),
            )
        ]
    finally:
        conn.close()


def _set_activity(dates, discord_id="alice", guild_id="guild-1"):
    conn = database.connect()
    try:
        user_id = conn.execute("SELECT user_id FROM users WHERE discord_id = ?", (discord_id,)).fetchone()[0]
        conn.execute("DELETE FROM reading_activity WHERE user_id = ? AND guild_id = ?", (user_id, guild_id))
        conn.executemany(
            "INSERT INTO reading_activity (user_id, guild_id, activity_date) VALUES (?, ?, ?)",
            [(user_id, guild_id, d) for d in dates],
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize(
    "utc_hour, local_day, activity_date, expected",
    [
        (3, dt.date(2026, 9, 25), "2026-09-26", 1),
        (23, dt.date(2026, 9, 27), "2026-09-25", 1),
        (3, dt.date(2026, 9, 25), "2026-09-24", 0),
    ],
    ids=["local-date-behind-utc", "local-date-ahead-of-utc", "expired-in-utc"],
)
def test_streak_uses_utc_day(db, monkeypatch, utc_hour, local_day, activity_date, expected):
    action_start_book("alice", "Alice", "guild-1", {"title": "Dune"})
    _set_activity([activity_date])

    utc_now = dt.datetime(2026, 9, 26, utc_hour, tzinfo=dt.timezone.utc)

    class FixedDatetime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return dt.datetime.combine(local_day, dt.time())
            return utc_now.astimezone(tz)

    monkeypatch.setattr(actions_engagement, "datetime", FixedDatetime)

    assert actions_engagement.get_streaks("alice", "guild-1") == {
        "current": expected,
        "longest": 1,
    }


def test_daily_progress_on_one_book_builds_streak(db, monkeypatch):
    start = dt.datetime(2026, 9, 20, 18, tzinfo=dt.timezone.utc)
    _freeze(monkeypatch, actions, start)
    action_start_book("alice", "Alice", "guild-1", {"title": "Dune"})
    for day in range(1, 4):
        _freeze(monkeypatch, actions, start + dt.timedelta(days=day))
        assert action_update_progress("alice", "Alice", "guild-1", {"title": "Dune", "progress": 10 * day})
    # A second update on the same UTC day is not a second day.
    assert action_update_progress("alice", "Alice", "guild-1", {"title": "Dune", "progress": 35})

    assert _activity_dates() == ["2026-09-20", "2026-09-21", "2026-09-22", "2026-09-23"]
    _freeze(monkeypatch, actions_engagement, start + dt.timedelta(days=3))
    assert actions_engagement.get_streaks("alice", "guild-1") == {"current": 4, "longest": 4}


def test_wishlist_add_is_not_a_reading_day(db):
    assert actions.action_add_to_wishlist("alice", "Alice", "guild-1", {"title": "Dune"})
    assert _activity_dates() == []


@pytest.mark.parametrize(
    "dates, expected",
    [
        ([], {"current": 0, "longest": 0}),
        (["2026-09-26", "2026-09-25", "2026-09-24"], {"current": 3, "longest": 3}),
        (["2026-09-25", "2026-09-24", "2026-09-23"], {"current": 3, "longest": 3}),
        (["2026-09-24", "2026-09-23"], {"current": 0, "longest": 2}),
        (["2026-09-26", "2026-09-20", "2026-09-19", "2026-09-18"], {"current": 1, "longest": 3}),
    ],
    ids=["none", "through-today", "through-yesterday", "lapsed", "longest-is-older"],
)
def test_streaks_from_dates(dates, expected):
    parsed = [dt.date.fromisoformat(d) for d in dates]
    assert actions_engagement._streaks_from_dates(parsed, dt.date(2026, 9, 26)) == expected


def test_migration_backfills_reading_days_only(db):
    action_start_book("alice", "Alice", "guild-1", {"title": "Dune"})
    actions.action_add_to_wishlist("alice", "Alice", "guild-1", {"title": "Emma"})
    conn = database.connect()
    try:
        user_id = conn.execute("SELECT user_id FROM users WHERE discord_id = 'alice'").fetchone()[0]
        book_id = conn.execute("SELECT book_id FROM books WHERE title = 'Dune'").fetchone()[0]
        conn.execute(
            """INSERT INTO reading_progress
               (user_id, book_id, guild_id, status, finished_at, last_activity_at, source)
               VALUES (?, ?, 'guild-1', 'finished', ?, ?, 'goodreads')""",
            (user_id, book_id, "2026-01-02 00:00:00", "2026-01-02 00:00:00"),
        )
        conn.execute(
            "UPDATE reading_progress SET last_activity_at = ? WHERE status = 'reading'",
            ("2026-09-25 23:30:00+00:00",),
        )
        conn.execute(
            "UPDATE reading_progress SET last_activity_at = ? WHERE status = 'wishlist'",
            ("2026-09-24 12:00:00+00:00",),
        )
        conn.execute("DELETE FROM reading_activity")
        database._create_reading_activity(conn)
        conn.commit()
    finally:
        conn.close()

    assert _activity_dates() == ["2026-09-25"]


def test_leaderboard_ranks_current_streaks_in_guild(db, monkeypatch):
    for discord_id, name in (("alice", "Alice"), ("bob", "Bob"), ("cara", "Cara")):
        action_start_book(discord_id, name, "guild-1", {"title": "Dune"})
    action_start_book("dan", "Dan", "guild-2", {"title": "Dune"})
    _set_activity(["2026-09-26"], "alice")
    _set_activity(["2026-09-26", "2026-09-25", "2026-09-24"], "bob")
    _set_activity(["2026-09-10"], "cara")
    _set_activity(["2026-09-26"], "dan", "guild-2")
    _freeze(monkeypatch, actions_engagement, dt.datetime(2026, 9, 26, 12, tzinfo=dt.timezone.utc))

    assert actions_engagement.get_streak_leaderboard("guild-1") == [
        {"display_name": "Bob", "current": 3, "longest": 3},
        {"display_name": "Alice", "current": 1, "longest": 1},
    ]


def test_user_context_reports_streak_and_goal_progress(db):
    from actions import action_finish_book, build_user_context
    from actions_engagement import action_set_goal

    action_start_book("alice", "Alice", "guild-1", {"title": "Dune"})
    action_finish_book("alice", "Alice", "guild-1", {"title": "Dune"})
    action_set_goal("alice", "Alice", {"goal": 12})

    context = actions.build_user_context("alice", "guild-1")
    assert context["streak"].startswith("You're on a 1-day reading streak")
    assert context["reading_goal"]["goal"] == 12
    assert context["reading_goal"]["progress"] == 1
    assert "streak" not in build_user_context("alice", "guild-2")
