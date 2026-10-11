"""Shared nomination/poll/coordination Discord orchestration."""
import asyncio
import os
import pathlib
import re
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
import jobs
import poll_workflows
from actions_nominations import (
    close_nomination_window,
    get_active_coordination_poll,
    get_active_poll,
    get_active_window,
    open_coordination_poll,
    open_nomination_window,
    open_poll,
    submit_nomination,
)
from actions_session import get_active_session
from poll_workflows import (
    close_book_poll_workflow,
    close_nominations_workflow,
    open_book_poll_workflow,
    open_coordination_poll_workflow,
    open_nominations_workflow,
)


GUILD = "42"
OPENER = "111"
_REAL_SCHEDULE_AUTO_CLOSE = jobs.schedule_auto_close
_REAL_SCHEDULE_POLL_AUTO_CLOSE = jobs.schedule_poll_auto_close
_REAL_SCHEDULE_COORDINATION_AUTO_CLOSE = jobs.schedule_coordination_poll_auto_close
DIRECT_ACTION_CALLS = re.compile(
    r"\b(open_nomination_window|close_nomination_window|open_poll|close_poll|"
    r"open_tiebreaker_poll|open_coordination_poll|close_coordination_poll)\("
)
CALLER_FILES = (
    "handlers.py",
    "commands.py",
    "views_dashboard.py",
)


class FakeChannel:
    def __init__(self, channel_id=1, name="nominations"):
        self.id = channel_id
        self.name = name


class FakeGuild:
    def __init__(self):
        self.id = int(GUILD)
        self.text_channels = [FakeChannel()]


def _run(coro):
    return asyncio.run(coro)


class PollWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_patch = mock.patch.object(
            database, "DB_PATH", os.path.join(self.tmpdir.name, "bookclub.db")
        )
        self.db_patch.start()
        database.init_db()
        self.guild = FakeGuild()
        self.named = self.guild.text_channels[0]
        self.fallback = FakeChannel(channel_id=99, name="general")
        self.discord_patch = mock.patch.multiple(
            poll_workflows,
            create_poll_thread=mock.AsyncMock(),
            send_nominations_summary=mock.AsyncMock(),
            send_poll_close=mock.AsyncMock(),
            send_current_read=mock.AsyncMock(),
            send_summary=mock.AsyncMock(),
            close_and_archive_poll_thread=mock.AsyncMock(return_value=None),
            resolve_poll_thread=mock.AsyncMock(return_value=None),
            resolve_text_channel=mock.Mock(return_value=self.named),
            _voting_view=mock.Mock(return_value=object()),
            _coordination_view=mock.Mock(return_value=object()),
        )
        self.discord_patch.start()
        self.job_patch = mock.patch.multiple(
            jobs,
            schedule_auto_close=mock.Mock(),
            cancel_auto_close=mock.Mock(),
            schedule_poll_auto_close=mock.Mock(),
            cancel_poll_auto_close=mock.Mock(),
            schedule_coordination_poll_auto_close=mock.Mock(),
            cancel_coordination_poll_auto_close=mock.Mock(),
            schedule_poll_thread_deletion=mock.Mock(),
            client=mock.Mock(),
        )
        self.job_patch.start()

    def tearDown(self):
        self.job_patch.stop()
        self.discord_patch.stop()
        self.db_patch.stop()
        self.tmpdir.cleanup()

    def test_open_nominations_schedules_and_posts_once(self):
        result = _run(open_nominations_workflow(self.guild, GUILD, OPENER, 5, self.fallback))
        self.assertTrue(result.ok)
        self.assertTrue(result.posted_elsewhere)
        self.assertIsNotNone(get_active_window(GUILD))
        jobs.schedule_auto_close.assert_called_once()
        poll_workflows.create_poll_thread.assert_awaited_once()
        parent = poll_workflows.create_poll_thread.await_args.args[0]
        self.assertIs(parent, self.named)
        announcement = poll_workflows.create_poll_thread.await_args.args[6]
        self.assertIn("/nominate", announcement)
        self.assertIn("Nominate button", announcement)
        view = poll_workflows.create_poll_thread.await_args.args[8]
        self.assertIsNotNone(view)

        again = _run(open_nominations_workflow(self.guild, GUILD, OPENER, 5, self.fallback))
        self.assertFalse(again.ok)
        self.assertEqual(again.reason, "already_open")
        self.assertEqual(poll_workflows.create_poll_thread.await_count, 1)
        self.assertEqual(jobs.schedule_auto_close.call_count, 1)

    def test_close_nominations_cancels_job_and_summarizes(self):
        _run(open_nominations_workflow(self.guild, GUILD, OPENER, 5, self.fallback))
        result = _run(close_nominations_workflow(self.guild, GUILD, self.fallback))
        self.assertTrue(result.ok)
        self.assertIsNone(get_active_window(GUILD))
        jobs.cancel_auto_close.assert_called_once_with(GUILD)
        poll_workflows.send_nominations_summary.assert_awaited_once()

        again = _run(close_nominations_workflow(self.guild, GUILD, self.fallback))
        self.assertFalse(again.ok)
        self.assertEqual(again.reason, "not_open")

    def test_open_book_poll_needs_closed_nominations(self):
        result = _run(open_book_poll_workflow(self.guild, GUILD, OPENER, 3, self.fallback))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "no_nominations")
        jobs.schedule_poll_auto_close.assert_not_called()

        window_id = open_nomination_window(GUILD, OPENER, days=1)
        submit_nomination(OPENER, "Alice", GUILD, "Dune", "Frank Herbert", "because spice")
        close_nomination_window(GUILD, manual=True)
        self.assertTrue(window_id)

        result = _run(open_book_poll_workflow(self.guild, GUILD, OPENER, 3, self.fallback))
        self.assertTrue(result.ok)
        self.assertIsNotNone(get_active_poll(GUILD))
        jobs.schedule_poll_auto_close.assert_called_once()
        poll_workflows.create_poll_thread.assert_awaited()
        poll_workflows._voting_view.assert_called_once()

        again = _run(open_book_poll_workflow(self.guild, GUILD, OPENER, 3, self.fallback))
        self.assertFalse(again.ok)
        self.assertEqual(again.reason, "already_open")

    def test_close_tied_poll_opens_tiebreaker(self):
        open_nomination_window(GUILD, OPENER, days=1)
        submit_nomination("1", "Alice", GUILD, "Dune", "Frank Herbert", "a")
        submit_nomination("2", "Bob", GUILD, "Neuromancer", "William Gibson", "b")
        close_nomination_window(GUILD, manual=True)
        poll_id, noms = open_poll(GUILD, OPENER, days=1)
        self.assertTrue(poll_id)
        from actions_nominations import cast_vote
        cast_vote("1", "Alice", GUILD, noms[0]["nomination_id"], poll_id)
        cast_vote("2", "Bob", GUILD, noms[1]["nomination_id"], poll_id)

        result = _run(close_book_poll_workflow(self.guild, GUILD, OPENER, self.fallback))
        self.assertTrue(result.ok)
        poll = get_active_poll(GUILD)
        self.assertIsNotNone(poll)
        self.assertTrue(poll["is_tiebreaker"])
        self.assertIsNone(get_active_window(GUILD))
        jobs.schedule_poll_auto_close.assert_called()
        thread_names = [call.args[4] for call in poll_workflows.create_poll_thread.await_args_list]
        self.assertIn("⚖️ Tiebreaker poll", thread_names)
        self.assertIsNone(get_active_session(GUILD))
        poll_workflows.send_current_read.assert_not_called()

    def test_close_winner_starts_club_read(self):
        from actions_nominations import cast_vote

        open_nomination_window(GUILD, OPENER, days=1)
        submit_nomination(OPENER, "Alice", GUILD, "Dune", "Frank Herbert", "spice")
        close_nomination_window(GUILD, manual=True)
        poll_id, noms = open_poll(GUILD, OPENER, days=1)
        cast_vote(OPENER, "Alice", GUILD, noms[0]["nomination_id"], poll_id)

        result = _run(close_book_poll_workflow(self.guild, GUILD, OPENER, self.fallback))
        self.assertTrue(result.ok)
        session = get_active_session(GUILD)
        self.assertIsNotNone(session)
        self.assertEqual(session["title"], "Dune")
        self.assertEqual(session["poll_id"], poll_id)
        poll_workflows.send_current_read.assert_awaited()

    def test_close_single_nomination_without_votes_does_not_start_club_read(self):
        open_nomination_window(GUILD, OPENER, days=1)
        submit_nomination(OPENER, "Alice", GUILD, "Dune", "Frank Herbert", "spice")
        close_nomination_window(GUILD, manual=True)
        open_poll(GUILD, OPENER, days=1)

        result = _run(close_book_poll_workflow(self.guild, GUILD, OPENER, self.fallback))

        self.assertTrue(result.ok)
        self.assertIsNone(get_active_session(GUILD))
        poll_workflows.send_current_read.assert_not_awaited()
        close_result = poll_workflows.send_poll_close.await_args.args[1]
        self.assertIsNone(close_result["winner"])
        self.assertFalse(close_result["tied"])

    def _auto_close_harness(self, registry_name):
        launched = []
        added = []

        class FakeScheduler:
            def get_job(self, job_id):
                return next((item for item in added if item["id"] == job_id), None)

            def remove_job(self, job_id):
                added[:] = [item for item in added if item["id"] != job_id]

            def add_job(self, func, *_args, **kwargs):
                job_id = kwargs["id"]
                added[:] = [item for item in added if item["id"] != job_id]
                added.append({"func": func, **kwargs})
                captured["job_fn"] = func
                return type("Job", (), {"id": job_id})()

        captured = {}

        def fake_launch(coro, label):
            launched.append((coro.cr_code.co_name, label))
            coro.close()

        return launched, added, captured, (
            mock.patch.object(jobs, "scheduler", FakeScheduler()),
            mock.patch.object(jobs, "_launch", fake_launch),
            mock.patch.object(jobs, registry_name, {}),
        )

    def test_schedule_poll_auto_close_launches_workflow_or_noops(self):
        launched, added, captured, patches = self._auto_close_harness("poll_jobs")
        with mock.patch.object(jobs, "schedule_poll_auto_close", _REAL_SCHEDULE_POLL_AUTO_CLOSE), \
             patches[0], patches[1], patches[2]:
            jobs.schedule_poll_auto_close(GUILD, "2099-01-01T00:00:00+00:00", self.guild)
            captured["job_fn"]()
        self.assertEqual(launched, [])
        self.assertEqual(added[0]["id"], f"auto-close:poll:{GUILD}")
        self.assertTrue(added[0]["replace_existing"])

        open_nomination_window(GUILD, OPENER, days=1)
        submit_nomination(OPENER, "Alice", GUILD, "Dune", "Frank Herbert", "because spice")
        close_nomination_window(GUILD, manual=True)
        poll_id, _noms = open_poll(GUILD, OPENER, days=1)
        self.assertTrue(poll_id)

        with mock.patch.object(jobs, "schedule_poll_auto_close", _REAL_SCHEDULE_POLL_AUTO_CLOSE), \
             patches[0], patches[1], patches[2]:
            jobs.schedule_poll_auto_close(GUILD, "2099-01-01T00:00:00+00:00", self.guild)
            captured["job_fn"]()
        self.assertEqual(launched, [("close_book_poll_workflow", "auto_close")])

    def test_overlapping_auto_close_schedules_replace_one_job(self):
        cases = (
            ("nominations", _REAL_SCHEDULE_AUTO_CLOSE, "nomination_jobs"),
            ("poll", _REAL_SCHEDULE_POLL_AUTO_CLOSE, "poll_jobs"),
            ("coordination", _REAL_SCHEDULE_COORDINATION_AUTO_CLOSE, "coordination_poll_jobs"),
        )
        closes_at = "2099-01-01T00:00:00+00:00"
        for kind, real_fn, registry_name in cases:
            launched, added, _captured, patches = self._auto_close_harness(registry_name)
            with patches[0], patches[1], patches[2]:
                real_fn(GUILD, closes_at, self.guild)
                real_fn(GUILD, closes_at, self.guild)
            self.assertEqual(len(added), 1, kind)
            self.assertEqual(added[0]["id"], f"auto-close:{kind}:{GUILD}", kind)
            self.assertTrue(added[0]["replace_existing"], kind)
            self.assertEqual(launched, [])

    def test_nomination_and_coordination_auto_close_noops_when_closed(self):
        launched, _added, captured, patches = self._auto_close_harness("nomination_jobs")
        with mock.patch.object(jobs, "schedule_auto_close", _REAL_SCHEDULE_AUTO_CLOSE), \
             patches[0], patches[1], patches[2]:
            jobs.schedule_auto_close(GUILD, "2099-01-01T00:00:00+00:00", self.guild)
            captured["job_fn"]()
        self.assertEqual(launched, [])

        launched, _added, captured, patches = self._auto_close_harness("coordination_poll_jobs")
        with mock.patch.object(jobs, "schedule_coordination_poll_auto_close", _REAL_SCHEDULE_COORDINATION_AUTO_CLOSE), \
             patches[0], patches[1], patches[2]:
            jobs.schedule_coordination_poll_auto_close(GUILD, "2099-01-01T00:00:00+00:00", self.guild)
            captured["job_fn"]()
        self.assertEqual(launched, [])
        self.assertIsNone(get_active_coordination_poll(GUILD))

        open_nomination_window(GUILD, OPENER, days=1)
        launched, _added, captured, patches = self._auto_close_harness("nomination_jobs")
        with mock.patch.object(jobs, "schedule_auto_close", _REAL_SCHEDULE_AUTO_CLOSE), \
             patches[0], patches[1], patches[2]:
            jobs.schedule_auto_close(GUILD, "2099-01-01T00:00:00+00:00", self.guild)
            captured["job_fn"]()
        self.assertEqual(launched, [("close_nominations_workflow", "auto_close")])

        open_coordination_poll(GUILD, OPENER, "When?", ["Mon", "Tue"], days=1)
        launched, _added, captured, patches = self._auto_close_harness("coordination_poll_jobs")
        with mock.patch.object(jobs, "schedule_coordination_poll_auto_close", _REAL_SCHEDULE_COORDINATION_AUTO_CLOSE), \
             patches[0], patches[1], patches[2]:
            jobs.schedule_coordination_poll_auto_close(GUILD, "2099-01-01T00:00:00+00:00", self.guild)
            captured["job_fn"]()
        self.assertEqual(launched, [("close_coordination_poll_workflow", "auto_close")])

    def test_jobs_and_bot_close_book_polls_through_the_workflow(self):
        jobs_src = pathlib.Path("jobs.py").read_text()
        bot_src = pathlib.Path("bot.py").read_text()
        self.assertNotIn("close_poll(", jobs_src)
        self.assertNotIn("close_poll(", bot_src)
        self.assertNotIn("close_nomination_window(", jobs_src)
        self.assertNotIn("close_coordination_poll(", jobs_src)
        self.assertIn("close_book_poll_workflow", jobs_src)
        self.assertIn("close_nominations_workflow", jobs_src)
        self.assertIn("close_coordination_poll_workflow", jobs_src)
        self.assertIn("close_book_poll_workflow", bot_src)
        self.assertIn("async with ready_lock():", bot_src)
        self.assertIn("await _on_ready_startup()", bot_src)

    def test_coordination_rejects_bad_options_before_db_write(self):
        result = _run(open_coordination_poll_workflow(
            self.guild, GUILD, OPENER, "When?", ["Monday"], 2, self.fallback,
        ))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "invalid_options")
        jobs.schedule_coordination_poll_auto_close.assert_not_called()

        result = _run(open_coordination_poll_workflow(
            self.guild, GUILD, OPENER, "When?", ["Monday", "Tuesday"], 2, self.fallback,
        ))
        self.assertTrue(result.ok)
        poll_workflows.create_poll_thread.assert_awaited_once()
        poll_workflows._coordination_view.assert_called_once()

    def test_callers_use_the_shared_helpers(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        for name in CALLER_FILES:
            src = (root / name).read_text()
            self.assertIn("from poll_workflows import", src, name)
            hits = DIRECT_ACTION_CALLS.findall(src)
            self.assertEqual(hits, [], msg=f"{name} still calls {hits} directly")


if __name__ == "__main__":
    unittest.main()
