"""Delivery of personal reminders stored in the events table."""
import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
from actions import get_or_create_user
from actions_nudges import list_due_reminders, mark_reminder_delivered
import jobs


class ReminderDeliveryTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_patch = mock.patch.object(
            database, "DB_PATH", os.path.join(self._tmpdir.name, "bookclub.db")
        )
        self._db_patch.start()
        database.init_db()
        conn = database.connect()
        self.user_id = get_or_create_user(conn, "11", "Alice")
        conn.commit()
        conn.close()

    def tearDown(self):
        self._db_patch.stop()
        self._tmpdir.cleanup()

    def _insert(self, scheduled, *, recurrence="once", active=1, message="Read Dune"):
        conn = database.connect()
        conn.execute(
            """INSERT INTO events
               (user_id, book_id, guild_id, message, scheduled_time, recurrence, type, active)
               VALUES (?, NULL, ?, ?, ?, ?, 'reminder', ?)""",
            (self.user_id, "guild-1", message, scheduled, recurrence, active),
        )
        conn.commit()
        event_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()
        return event_id

    def test_past_once_reminder_is_due_then_deactivated(self):
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        event_id = self._insert(past)
        due = list_due_reminders()
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["message"], "Read Dune")
        mark_reminder_delivered(event_id, "once", due[0]["scheduled_time"])
        self.assertEqual(list_due_reminders(), [])

    def test_future_reminder_is_not_due(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        self._insert(future)
        self.assertEqual(list_due_reminders(), [])

    def test_daily_reminder_advances_past_now(self):
        past = (datetime.now(timezone.utc) - timedelta(days=2, minutes=1)).isoformat()
        event_id = self._insert(past, recurrence="daily")
        due = list_due_reminders()
        self.assertEqual(len(due), 1)
        mark_reminder_delivered(event_id, "daily", due[0]["scheduled_time"])
        self.assertEqual(list_due_reminders(), [])
        conn = database.connect()
        row = conn.execute(
            "SELECT scheduled_time, active FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
        conn.close()
        self.assertEqual(row[1], 1)
        nxt = datetime.fromisoformat(row[0])
        self.assertGreater(nxt, datetime.now(timezone.utc))

    def test_run_due_reminders_dms_then_deactivates(self):
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        self._insert(past, message="Pick up Piranesi")
        sent = []

        class FakeUser:
            async def send(self, **kwargs):
                sent.append(kwargs["embed"].description)

        fake_client = SimpleNamespace(
            fetch_user=mock.AsyncMock(return_value=FakeUser()),
        )
        with mock.patch.object(jobs, "client", fake_client):
            asyncio.run(jobs.run_due_reminders())
        self.assertEqual(sent, ["Pick up Piranesi"])
        self.assertEqual(list_due_reminders(), [])
        fake_client.fetch_user.assert_awaited()


if __name__ == "__main__":
    unittest.main()
