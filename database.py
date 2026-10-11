import sqlite3
import os

DB_PATH = os.path.join("data", "bookclub.db")
SQLITE_BUSY_TIMEOUT_SECONDS = 5.0


def _create_guild_release_announcements(conn):
    """Add one delivery record per guild and announced application version."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS guild_release_announcements (
               guild_id TEXT NOT NULL,
               version TEXT NOT NULL,
               announced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
               PRIMARY KEY (guild_id, version)
           )"""
    )


def _create_guild_release_announcement_settings(conn):
    """Add per-guild opt-out settings for member-facing release notes."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS guild_release_announcement_settings (
               guild_id TEXT PRIMARY KEY,
               enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
               updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
           )"""
    )


def _create_guild_settings(conn):
    """Per-guild club settings. Rows are inserted by ensure_guild_settings, not here."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS guild_settings (
               guild_id TEXT PRIMARY KEY,
               member_opt_in_enabled INTEGER NOT NULL CHECK (member_opt_in_enabled IN (0, 1)),
               weekly_recap_scheduled INTEGER NOT NULL CHECK (weekly_recap_scheduled IN (0, 1)),
               checkin_prompt_scheduled INTEGER NOT NULL CHECK (checkin_prompt_scheduled IN (0, 1)),
               combined_weekly_post INTEGER NOT NULL CHECK (combined_weekly_post IN (0, 1)),
               taste_profile_scheduled INTEGER NOT NULL CHECK (taste_profile_scheduled IN (0, 1)),
               inactivity_nudge_scheduled INTEGER NOT NULL CHECK (inactivity_nudge_scheduled IN (0, 1)),
               listen_channel_only INTEGER NOT NULL CHECK (listen_channel_only IN (0, 1)),
               require_mention INTEGER NOT NULL CHECK (require_mention IN (0, 1)),
               listen_channel_name TEXT NOT NULL,
               reply_channel_name TEXT NOT NULL,
               opt_in_welcome_channel TEXT NOT NULL,
               nominations_channel TEXT NOT NULL,
               weekly_recap_channel TEXT NOT NULL,
               checkin_prompt_channel TEXT NOT NULL,
               leaderboard_channel TEXT NOT NULL,
               year_in_review_channel TEXT NOT NULL,
               release_announcements_channel TEXT NOT NULL,
               weekly_recap_day_of_week TEXT NOT NULL,
               weekly_recap_hour INTEGER NOT NULL,
               checkin_prompt_day_of_week TEXT NOT NULL,
               checkin_prompt_hour INTEGER NOT NULL,
               inactivity_nudge_days INTEGER NOT NULL,
               inactivity_nudge_interval_hours INTEGER NOT NULL,
               nominations_default_days INTEGER NOT NULL,
               poll_default_days INTEGER NOT NULL,
               coordination_poll_default_days INTEGER NOT NULL,
               thread_auto_archive_minutes INTEGER NOT NULL,
               poll_thread_retention_days INTEGER NOT NULL DEFAULT 7,
               created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
               updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
           )"""
    )


def _add_enrollment_channel_id(conn):
    """Track the channel containing each guild's persistent enrollment panel."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(servers)")}
    if "enrollment_channel_id" not in columns:
        conn.execute("ALTER TABLE servers ADD COLUMN enrollment_channel_id TEXT")


def _add_release_board_message_ids(conn):
    """Track the persistent per-guild changelog embed message and channel."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(servers)")}
    if "release_announcement_message_id" not in columns:
        conn.execute("ALTER TABLE servers ADD COLUMN release_announcement_message_id TEXT")
    if "release_announcement_channel_id" not in columns:
        conn.execute("ALTER TABLE servers ADD COLUMN release_announcement_channel_id TEXT")


def _add_ratings_source(conn):
    """Optional provenance so imported Goodreads scores stay off club recaps."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(ratings)")}
    if "source" not in columns:
        conn.execute("ALTER TABLE ratings ADD COLUMN source TEXT")


def _add_poll_thread_retention(conn):
    """Add per-guild retention for completed temporary poll threads."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(guild_settings)")}
    if "poll_thread_retention_days" not in columns:
        conn.execute(
            "ALTER TABLE guild_settings ADD COLUMN poll_thread_retention_days INTEGER NOT NULL DEFAULT 7"
        )


def _create_poll_threads(conn):
    """Persist Discord poll-thread ids and restart-safe cleanup state."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS poll_threads (
               guild_id TEXT NOT NULL,
               workflow_type TEXT NOT NULL,
               workflow_id INTEGER NOT NULL,
               parent_channel_id TEXT NOT NULL,
               anchor_message_id TEXT NOT NULL,
               thread_id TEXT NOT NULL,
               status TEXT NOT NULL DEFAULT 'open',
               closed_at TIMESTAMP,
               delete_after TIMESTAMP,
               deleted_at TIMESTAMP,
               PRIMARY KEY (guild_id, workflow_type, workflow_id)
           )"""
    )


def _create_managed_roles(conn):
    """Store per-guild Discord IDs for the bot's managed participation roles."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS managed_roles (
               guild_id TEXT NOT NULL,
               role_key TEXT NOT NULL,
               role_id TEXT NOT NULL,
               updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
               PRIMARY KEY (guild_id, role_key)
           )"""
    )


def _create_role_sync_state(conn):
    """Track completion of the one-time participation-role pass per guild."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS role_sync_state (
               guild_id TEXT PRIMARY KEY,
               completed_at TIMESTAMP
           )"""
    )


def _migrate_member_features_to_participation_keys(conn):
    """Replace the core preference with the four-key participation model.

    Core members map to Tracking only: the migration never subscribes anyone to
    higher-volume Updates or Club notifications. DM Nudges now requires
    Tracking; legacy invariants already forced nudges to imply Core, so this
    only removes rows that violate the new rule.
    """
    conn.execute(
        """UPDATE member_feature_preferences
           SET feature_key = 'tracking', updated_at = CURRENT_TIMESTAMP
           WHERE feature_key = 'core' AND enabled = 1"""
    )
    conn.execute("DELETE FROM member_feature_preferences WHERE feature_key = 'core'")
    conn.execute(
        """DELETE FROM member_feature_preferences
           WHERE feature_key = 'dm_nudges'
             AND NOT EXISTS (
                 SELECT 1 FROM member_feature_preferences AS tracking_row
                 WHERE tracking_row.guild_id = member_feature_preferences.guild_id
                   AND tracking_row.user_id = member_feature_preferences.user_id
                   AND tracking_row.feature_key = 'tracking'
                   AND tracking_row.enabled = 1
             )"""
    )


def _participation_roles_migration(conn):
    """Migration 7: managed-role registry, sync marker, and feature-key rewrite."""
    _create_managed_roles(conn)
    _create_role_sync_state(conn)
    _migrate_member_features_to_participation_keys(conn)


def _create_reading_queue(conn):
    """Private per-member TBR queue, separate from the public wishlist shelf."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS reading_queue (
               queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
               user_id INTEGER NOT NULL REFERENCES users(user_id),
               guild_id TEXT NOT NULL,
               book_id INTEGER NOT NULL REFERENCES books(book_id),
               source TEXT,
               created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
               UNIQUE (guild_id, user_id, book_id)
           )"""
    )


def _add_reading_progress_source(conn):
    """Optional provenance for wishlist (and other) shelf inserts."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(reading_progress)")}
    if "source" not in columns:
        conn.execute("ALTER TABLE reading_progress ADD COLUMN source TEXT")


def _personal_reading_queues_migration(conn):
    """Migration 8: private reading_queue table + wishlist source column."""
    _create_reading_queue(conn)
    _add_reading_progress_source(conn)


def _create_reading_sessions(conn):
    """One canonical club read per guild, with optional target date."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS reading_sessions (
               session_id INTEGER PRIMARY KEY AUTOINCREMENT,
               guild_id TEXT NOT NULL,
               book_id INTEGER NOT NULL REFERENCES books(book_id),
               status TEXT NOT NULL CHECK (status IN ('active', 'completed')),
               started_at TIMESTAMP NOT NULL,
               target_date TEXT,
               started_by TEXT,
               poll_id INTEGER,
               completed_at TIMESTAMP,
               created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
           )"""
    )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS reading_sessions_one_active_per_guild
           ON reading_sessions(guild_id) WHERE status = 'active'"""
    )


def _create_scrapbook_entries(conn):
    """Append-only member notes attached to a specific shared reading session."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS scrapbook_entries (
               entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
               session_id INTEGER NOT NULL REFERENCES reading_sessions(session_id),
               discord_id TEXT NOT NULL,
               display_name TEXT NOT NULL,
               kind TEXT NOT NULL CHECK (kind IN (
                   'quote', 'highlight', 'reaction', 'rating_note', 'discussion'
               )),
               content TEXT NOT NULL,
               created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
           )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS scrapbook_entries_session_created
           ON scrapbook_entries(session_id, created_at, entry_id)"""
    )


def _create_reading_activity(conn):
    """One row per member, guild, and UTC day with reading activity (streaks).

    Backfills from the one surviving timestamp per shelf row, so streaks recorded
    before this migration are incomplete. Wishlist adds and imported history do
    not count as reading days.
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS reading_activity (
               user_id INTEGER NOT NULL REFERENCES users(user_id),
               guild_id TEXT NOT NULL,
               activity_date TEXT NOT NULL,
               PRIMARY KEY (user_id, guild_id, activity_date)
           )"""
    )
    conn.execute(
        """INSERT OR IGNORE INTO reading_activity (user_id, guild_id, activity_date)
           SELECT DISTINCT user_id, guild_id, DATE(last_activity_at)
           FROM reading_progress
           WHERE guild_id IS NOT NULL
             AND last_activity_at IS NOT NULL
             AND DATE(last_activity_at) IS NOT NULL
             AND status != 'wishlist'
             AND COALESCE(source, '') != 'goodreads'"""
    )


def _index_reading_progress_user_guild(conn):
    """Per-message chat context reads a member's shelf by (user_id, guild_id)."""
    conn.execute(
        """CREATE INDEX IF NOT EXISTS reading_progress_user_guild
           ON reading_progress(user_id, guild_id)"""
    )


# Migrations are intentionally append-only.  Never change a migration that may
# already have been recorded in a deployed database; add the next version instead.
MIGRATIONS = (
    (1, _create_guild_release_announcements),
    (2, _create_guild_release_announcement_settings),
    (3, _create_guild_settings),
    (4, _add_enrollment_channel_id),
    (5, _add_poll_thread_retention),
    (6, _create_poll_threads),
    (7, _participation_roles_migration),
    (8, _personal_reading_queues_migration),
    (9, _create_reading_sessions),
    (10, _add_release_board_message_ids),
    (11, _add_ratings_source),
    (12, _create_scrapbook_entries),
    (13, _create_reading_activity),
    (14, _index_reading_progress_user_guild),
)


def _apply_migrations(conn):
    """Apply each pending schema migration exactly once, in version order."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_version (
               version INTEGER PRIMARY KEY,
               applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
           )"""
    )
    applied_versions = {
        row[0] for row in conn.execute("SELECT version FROM schema_version")
    }

    for version, migration in MIGRATIONS:
        if version in applied_versions:
            continue
        migration(conn)
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))


def connect():
    """Open a connection to the configured database.

    The path is resolved at call time, so tests can redirect the database by
    patching database.DB_PATH (a previously imported DB_PATH binding would be
    frozen at import time and could not be redirected).
    """
    # Wait on a locked database (scheduled job vs chat write) instead of failing.
    conn = sqlite3.connect(DB_PATH, timeout=SQLITE_BUSY_TIMEOUT_SECONDS)
    if os.environ.get("BOOKCLUB_TEST_FAST_SQLITE"):
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("PRAGMA journal_mode=MEMORY")
        conn.execute("PRAGMA temp_store=MEMORY")
    return conn


def init_db():
    os.makedirs("data", exist_ok=True)

    conn = connect()
    if not os.environ.get("BOOKCLUB_TEST_FAST_SQLITE"):
        # Persistent on the file: readers no longer block the writer, or vice versa.
        conn.execute("PRAGMA journal_mode=WAL")

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_id TEXT UNIQUE NOT NULL,
            display_name TEXT,
            access_tier TEXT DEFAULT 'core',
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            taste_profile TEXT
        );

        CREATE TABLE IF NOT EXISTS servers (
            server_id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_guild_id TEXT UNIQUE NOT NULL,
            name TEXT,
            enrollment_message_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS books (
            book_id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            author TEXT,
            genre TEXT,
            page_count INTEGER,
            added_by INTEGER REFERENCES users(user_id),
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS reading_progress (
            progress_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER REFERENCES users(user_id),
            book_id INTEGER REFERENCES books(book_id),
            guild_id TEXT,
            status TEXT,
            progress_raw INTEGER,
            progress_unit TEXT,
            progress_pct INTEGER,
            notes TEXT,
            read_count INTEGER DEFAULT 1,
            started_at TIMESTAMP,
            finished_at TIMESTAMP,
            last_activity_at TIMESTAMP,
            last_nudged_at TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS ratings (
            rating_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER REFERENCES users(user_id),
            book_id INTEGER REFERENCES books(book_id),
            score INTEGER,
            review_notes TEXT,
            rated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER REFERENCES users(user_id),
            book_id INTEGER REFERENCES books(book_id),
            guild_id TEXT,
            message TEXT,
            scheduled_time TIMESTAMP,
            recurrence TEXT,
            type TEXT,
            active BOOLEAN DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS nominations (
            nomination_id INTEGER PRIMARY KEY AUTOINCREMENT,
            server_id INTEGER REFERENCES servers(server_id),
            book_id INTEGER REFERENCES books(book_id),
            nominated_by INTEGER REFERENCES users(user_id),
            nomination_reason TEXT,
            nominated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'active'
        );

        CREATE TABLE IF NOT EXISTS achievements (
            achievement_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER REFERENCES users(user_id),
            guild_id TEXT,
            achievement_type TEXT,
            title TEXT,
            description TEXT,
            earned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS event_log (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER REFERENCES users(user_id),
            guild_id TEXT,
            event_type TEXT,
            payload TEXT,
            logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS nomination_windows (
            window_id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id TEXT NOT NULL,
            opened_by TEXT NOT NULL,
            opens_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            closes_at TIMESTAMP NOT NULL,
            status TEXT DEFAULT 'open',
            closed_manually INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS polls (
            poll_id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id TEXT NOT NULL,
            window_id INTEGER REFERENCES nomination_windows(window_id),
            opened_by TEXT NOT NULL,
            opens_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            closes_at TIMESTAMP NOT NULL,
            status TEXT DEFAULT 'open',
            is_tiebreaker INTEGER DEFAULT 0,
            closed_manually INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS votes (
            vote_id INTEGER PRIMARY KEY AUTOINCREMENT,
            poll_id INTEGER REFERENCES polls(poll_id),
            user_id INTEGER REFERENCES users(user_id),
            nomination_id INTEGER REFERENCES nominations(nomination_id),
            voted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS coordination_polls (
            poll_id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id TEXT NOT NULL,
            question TEXT NOT NULL,
            opened_by TEXT NOT NULL,
            opens_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            closes_at TIMESTAMP NOT NULL,
            status TEXT DEFAULT 'open',
            closed_manually INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS coordination_options (
            option_id INTEGER PRIMARY KEY AUTOINCREMENT,
            poll_id INTEGER REFERENCES coordination_polls(poll_id),
            label TEXT NOT NULL,
            display_order INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS coordination_votes (
            vote_id INTEGER PRIMARY KEY AUTOINCREMENT,
            poll_id INTEGER REFERENCES coordination_polls(poll_id),
            user_id INTEGER REFERENCES users(user_id),
            option_id INTEGER REFERENCES coordination_options(option_id),
            voted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS user_tone_overrides (
            override_id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id TEXT NOT NULL,
            user_id INTEGER REFERENCES users(user_id),
            mode TEXT NOT NULL,
            updated_by TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(guild_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS member_feature_preferences (
            preference_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER REFERENCES users(user_id),
            guild_id TEXT NOT NULL,
            feature_key TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(guild_id, user_id, feature_key)
        );
    """)

    # Add window_id column to nominations if it doesn't exist (migration)
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(nominations)")
    cols = [row[1] for row in cur.fetchall()]
    if "window_id" not in cols:
        cur.execute("ALTER TABLE nominations ADD COLUMN window_id INTEGER")

    cur.execute("PRAGMA table_info(reading_progress)")
    progress_cols = [row[1] for row in cur.fetchall()]
    if "last_activity_at" not in progress_cols:
        cur.execute("ALTER TABLE reading_progress ADD COLUMN last_activity_at TIMESTAMP")
    if "last_nudged_at" not in progress_cols:
        cur.execute("ALTER TABLE reading_progress ADD COLUMN last_nudged_at TIMESTAMP")
    cur.execute(
        """UPDATE reading_progress
           SET last_activity_at = COALESCE(last_activity_at, finished_at, started_at, CURRENT_TIMESTAMP)
           WHERE last_activity_at IS NULL"""
    )

    cur.execute("PRAGMA table_info(users)")
    user_cols = [row[1] for row in cur.fetchall()]
    if "reading_goal" not in user_cols:
        cur.execute("ALTER TABLE users ADD COLUMN reading_goal INTEGER")
    if "goal_year" not in user_cols:
        cur.execute("ALTER TABLE users ADD COLUMN goal_year INTEGER")

    cur.execute("PRAGMA table_info(servers)")
    server_cols = [row[1] for row in cur.fetchall()]
    if "enrollment_message_id" not in server_cols:
        cur.execute("ALTER TABLE servers ADD COLUMN enrollment_message_id TEXT")

    cur.execute("PRAGMA table_info(achievements)")
    achievement_cols = [row[1] for row in cur.fetchall()]
    if "guild_id" not in achievement_cols:
        cur.execute("ALTER TABLE achievements ADD COLUMN guild_id TEXT")

    _apply_migrations(conn)
    conn.commit()
    conn.close()


def upsert_server(discord_guild_id: str, name: str):
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO servers (discord_guild_id, name)
               VALUES (?, ?)
               ON CONFLICT(discord_guild_id) DO UPDATE SET name = excluded.name""",
            (discord_guild_id, name)
        )
        conn.commit()
    finally:
        conn.close()


def get_enrollment_panel(discord_guild_id: str):
    conn = connect()
    try:
        row = conn.execute(
            "SELECT enrollment_message_id, enrollment_channel_id FROM servers WHERE discord_guild_id = ?",
            (discord_guild_id,),
        ).fetchone()
        return {"message_id": row[0], "channel_id": row[1]} if row else None
    finally:
        conn.close()


def set_enrollment_message_id(
    discord_guild_id: str,
    message_id: str | None = None,
    channel_id: str | None = None,
):
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE servers SET enrollment_message_id = ?, enrollment_channel_id = ? WHERE discord_guild_id = ?",
            (
                str(message_id) if message_id else None,
                str(channel_id) if channel_id else None,
                discord_guild_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_release_panel(discord_guild_id: str):
    conn = connect()
    try:
        row = conn.execute(
            """SELECT release_announcement_message_id, release_announcement_channel_id
               FROM servers WHERE discord_guild_id = ?""",
            (discord_guild_id,),
        ).fetchone()
        return {"message_id": row[0], "channel_id": row[1]} if row else None
    finally:
        conn.close()


def set_release_panel_message_id(
    discord_guild_id: str,
    message_id: str | None = None,
    channel_id: str | None = None,
):
    conn = connect()
    try:
        conn.execute(
            """UPDATE servers
               SET release_announcement_message_id = ?,
                   release_announcement_channel_id = ?
               WHERE discord_guild_id = ?""",
            (
                str(message_id) if message_id else None,
                str(channel_id) if channel_id else None,
                discord_guild_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()
