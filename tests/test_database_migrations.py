"""Offline coverage for the ordered SQLite migration runner."""
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database


class DatabaseMigrationTests(unittest.TestCase):
    def test_init_db_enables_wal_and_indexes_member_shelf_lookup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "bookclub.db")
            with mock.patch.object(database, "DB_PATH", db_path), \
                 mock.patch.dict(os.environ, {"BOOKCLUB_TEST_FAST_SQLITE": ""}):
                database.init_db()
                conn = database.connect()
                try:
                    self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
                    self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
                    plan = " ".join(
                        str(row[-1]) for row in conn.execute(
                            "EXPLAIN QUERY PLAN SELECT * FROM reading_progress "
                            "WHERE user_id = 1 AND guild_id = 'g'"
                        )
                    )
                    self.assertIn("reading_progress_user_guild", plan)
                finally:
                    conn.close()

    def test_init_db_records_and_applies_release_announcement_migration(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "bookclub.db")
            with mock.patch.object(database, "DB_PATH", db_path):
                database.init_db()

                conn = sqlite3.connect(db_path)
                try:
                    versions = conn.execute(
                        "SELECT version FROM schema_version ORDER BY version"
                    ).fetchall()
                    self.assertEqual(
                        versions, [(1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,), (9,), (10,), (11,), (12,), (13,), (14,)]
                    )

                    columns = conn.execute(
                        "PRAGMA table_info(guild_release_announcements)"
                    ).fetchall()
                    self.assertEqual(
                        {column[1] for column in columns},
                        {"guild_id", "version", "announced_at"},
                    )

                    queue_columns = {
                        row[1]
                        for row in conn.execute("PRAGMA table_info(reading_queue)").fetchall()
                    }
                    self.assertEqual(
                        queue_columns,
                        {
                            "queue_id",
                            "user_id",
                            "guild_id",
                            "book_id",
                            "source",
                            "created_at",
                        },
                    )
                    progress_columns = {
                        row[1]
                        for row in conn.execute(
                            "PRAGMA table_info(reading_progress)"
                        ).fetchall()
                    }
                    self.assertIn("source", progress_columns)
                    rating_columns = {
                        row[1]
                        for row in conn.execute("PRAGMA table_info(ratings)").fetchall()
                    }
                    self.assertIn("source", rating_columns)

                    server_columns = {
                        row[1]
                        for row in conn.execute("PRAGMA table_info(servers)").fetchall()
                    }
                    self.assertIn("release_announcement_message_id", server_columns)
                    self.assertIn("release_announcement_channel_id", server_columns)

                    session_columns = {
                        row[1]
                        for row in conn.execute(
                            "PRAGMA table_info(reading_sessions)"
                        ).fetchall()
                    }
                    self.assertEqual(
                        session_columns,
                        {
                            "session_id",
                            "guild_id",
                            "book_id",
                            "status",
                            "started_at",
                            "target_date",
                            "started_by",
                            "poll_id",
                            "completed_at",
                            "created_at",
                        },
                    )
                finally:
                    conn.close()

    def test_init_db_does_not_reapply_recorded_migrations(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "bookclub.db")
            with mock.patch.object(database, "DB_PATH", db_path):
                database.init_db()
                database.init_db()

                conn = sqlite3.connect(db_path)
                try:
                    versions = conn.execute(
                        "SELECT version FROM schema_version"
                    ).fetchall()
                    self.assertEqual(
                        versions, [(1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,), (9,), (10,), (11,), (12,), (13,), (14,)]
                    )
                finally:
                    conn.close()

    def test_participation_migration_maps_legacy_preferences(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "bookclub.db")
            with mock.patch.object(database, "DB_PATH", db_path):
                database.init_db()
                conn = sqlite3.connect(db_path)
                try:
                    conn.executescript(
                        """
                        INSERT INTO users (discord_id, display_name) VALUES
                            ('1', 'Core Nudges'), ('2', 'Orphan Nudges'), ('3', 'Core Only');
                        INSERT INTO member_feature_preferences (user_id, guild_id, feature_key, enabled) VALUES
                            (1, 'g1', 'core', 1), (1, 'g1', 'dm_nudges', 1),
                            (2, 'g1', 'core', 0), (2, 'g1', 'dm_nudges', 1),
                            (3, 'g1', 'core', 1);
                        """
                    )
                    # Replay only migration 7 against legacy-shaped rows.
                    conn.execute("DELETE FROM schema_version WHERE version = 7")
                    conn.execute("DROP TABLE managed_roles")
                    conn.execute("DROP TABLE role_sync_state")
                    conn.execute(
                        "UPDATE member_feature_preferences SET feature_key = 'core' WHERE feature_key = 'tracking'"
                    )
                    database._participation_roles_migration(conn)
                    rows = conn.execute(
                        """SELECT user_id, feature_key, enabled FROM member_feature_preferences
                           ORDER BY user_id, feature_key"""
                    ).fetchall()
                    self.assertEqual(
                        rows,
                        [
                            (1, "dm_nudges", 1),
                            (1, "tracking", 1),
                            (3, "tracking", 1),
                        ],
                    )
                    role_columns = {
                        column[1]
                        for column in conn.execute("PRAGMA table_info(managed_roles)").fetchall()
                    }
                    self.assertEqual(role_columns, {"guild_id", "role_key", "role_id", "updated_at"})
                    sync_columns = {
                        column[1]
                        for column in conn.execute("PRAGMA table_info(role_sync_state)").fetchall()
                    }
                    self.assertEqual(sync_columns, {"guild_id", "completed_at"})
                finally:
                    conn.close()


if __name__ == "__main__":
    unittest.main()
