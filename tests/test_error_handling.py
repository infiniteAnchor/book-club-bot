"""Offline tests for centralized error handling (no API, no real Discord I/O).

Covers:
- unexpected natural-language handler exceptions (fallback + full-context logs)
- LLM/API failures (fallback intent, admin alert wiring, cooldown)
- slash-command response vs follow-up behavior
- scheduled-job isolation
- secret/traceback/path redaction
"""
import asyncio
import concurrent.futures
import logging
import os
import sys
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock
from typing import Any, cast

import discord

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import errors


class CapturingHandler(logging.Handler):
    """Capture formatted log output (including tracebacks) for assertions."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.formatted = []
        self.records = []
        self.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))

    def emit(self, record):
        self.records.append(record)
        self.formatted.append(self.format(record))

    @property
    def text(self):
        return "\n".join(self.formatted)


class LogCapture:
    def __init__(self, logger_name="bookclub"):
        self.logger = logging.getLogger(logger_name)
        self.handler = CapturingHandler()

    def __enter__(self):
        self.logger.addHandler(self.handler)
        return self.handler

    def __exit__(self, *exc):
        self.logger.removeHandler(self.handler)
        return False


class FakeChannel:
    def __init__(self, fail=False):
        self.sent = []
        self._fail = fail

    async def send(self, content=None, **kwargs):
        if self._fail:
            raise RuntimeError("send failed")
        self.sent.append((content, kwargs))


class FakeMessage:
    def __init__(self, channel):
        self.channel = channel


class FakeResponse:
    def __init__(self, done=False):
        self._done = done
        self.calls = []

    def is_done(self):
        return self._done

    async def send_message(self, *args, **kwargs):
        self.calls.append(("send_message", args, kwargs))


class FakeFollowup:
    def __init__(self):
        self.calls = []

    async def send(self, *args, **kwargs):
        self.calls.append(("followup.send", args, kwargs))


class FakeUser(SimpleNamespace):
    def __str__(self):
        return "Reader#0001"


class FakeInteraction:
    def __init__(self, *, done=False, guild_id=987, user_id=555, command_name="poll"):
        self.response = FakeResponse(done=done)
        self.followup = FakeFollowup()
        self.guild_id = guild_id
        self.user = FakeUser(id=user_id, name="Reader", display_name="Reader")
        self.command = SimpleNamespace(qualified_name=command_name)


def make_secret_exception():
    """An exception whose message embeds everything users must never see.

    Raised and caught so it carries a real __traceback__, like production.
    """
    try:
        raise RuntimeError(
            "boom SELECT * FROM users WHERE api_key=sk-abcdef1234567890 "  # gitleaks:allow (fake key)
            "/opt/example-app/database.py"
        )
    except RuntimeError as exc:
        return exc


class RedactionTests(unittest.TestCase):
    def test_redacts_api_key_tokens(self):
        out = errors.redact("failed with key sk-abcdef1234567890 please retry")
        self.assertNotIn("sk-abcdef1234567890", out)
        self.assertIn("[REDACTED]", out)

    def test_redacts_assignment_style_secrets(self):
        out = errors.redact("request rejected: api_key = supersecretvalue123")
        self.assertNotIn("supersecretvalue123", out)

    def test_redacts_unix_and_windows_paths(self):
        out = errors.redact("see /opt/example-app/database.py and C:\\Users\\alice\\secret.txt")
        self.assertNotIn("/opt/example-app", out)
        self.assertNotIn("C:\\Users\\alice", out)
        self.assertIn("[PATH]", out)

    def test_redacts_traceback_header(self):
        out = errors.redact("Traceback (most recent call last):\n boom")
        self.assertNotIn("Traceback (most recent call last)", out)

    def test_redacts_configured_provider_keys(self):
        with mock.patch.object(errors, "SECRET_VALUES", ("sk-live-key-value-000111",)):
            out = errors.redact("bad key: sk-live-key-value-000111 end")
        self.assertNotIn("sk-live-key-value-000111", out)

    def test_redacts_configured_discord_and_google_values(self):
        values = ("plain-discord-token-value", "plain-google-key-value")
        with mock.patch.object(errors, "SECRET_VALUES", values):
            out = errors.redact_secrets("got plain-discord-token-value and plain-google-key-value")
        self.assertNotIn("plain-discord-token-value", out)
        self.assertNotIn("plain-google-key-value", out)

    def test_redacts_discord_and_google_shaped_tokens(self):
        # Assembled at runtime so secret scanners don't flag the fake fixture.
        discord_token = ".".join(["MTIzNDU2Nzg5MDEyMzQ1Njc4", "GAbCdE", "abcdefghijklmnopqrstuvwxyz0123456789"])
        google_key = "AIza" + "A1b2C3d4E5" * 3 + "xyzAB"
        out = errors.redact_secrets(f"login {discord_token} books {google_key}")
        self.assertNotIn(discord_token, out)
        self.assertNotIn(google_key, out)


class RedactingFormatterTests(unittest.TestCase):
    def _format_exception(self, secret):
        record_logger = logging.getLogger("bookclub.redaction-test")
        handler = CapturingHandler()
        handler.setFormatter(errors.RedactingFormatter("%(levelname)s %(name)s: %(message)s"))
        record_logger.addHandler(handler)
        record_logger.propagate = False
        try:
            try:
                raise RuntimeError(f"provider rejected {secret}")
            except RuntimeError:
                record_logger.exception("call failed for %s", secret)
        finally:
            record_logger.removeHandler(handler)
        return handler.formatted[0]

    def test_configured_secret_absent_from_message_and_traceback(self):
        secret = "fake-google-books-key-42"
        with mock.patch.object(errors, "SECRET_VALUES", (secret,)):
            out = self._format_exception(secret)
        self.assertNotIn(secret, out)
        self.assertIn("Traceback (most recent call last)", out)
        self.assertIn("RuntimeError: provider rejected [REDACTED]", out)

    def test_install_keeps_format_and_redacts(self):
        root = logging.getLogger()
        handler = CapturingHandler()
        handler.setFormatter(logging.Formatter("X %(message)s"))
        with mock.patch.object(root, "handlers", [handler]), \
             mock.patch.object(errors, "SECRET_VALUES", ("fake-discord-token-99",)):
            errors.install_log_redaction()
            record = logging.LogRecord("t", logging.INFO, __file__, 1, "tok fake-discord-token-99", None, None)
            out = handler.format(record)
        self.assertIsInstance(handler.formatter, errors.RedactingFormatter)
        self.assertEqual(out, "X tok [REDACTED]")

    def test_friendly_message_is_static_and_safe(self):
        msg = errors.FRIENDLY_ERROR_MESSAGE
        for banned in ("Traceback", "SELECT", "sk-", "/opt/", "{", "}"):
            self.assertNotIn(banned, msg)


class NaturalLanguageFailureTests(unittest.TestCase):
    def test_unexpected_handler_error_sends_static_fallback(self):
        channel = FakeChannel()
        message = FakeMessage(channel)
        with mock.patch.object(errors, "get_reply_channel", return_value=channel), LogCapture() as captured:
            asyncio.run(errors.handle_message_failure(
                message,
                make_secret_exception(),
                action="nominate",
                guild_id="424242",
                user="Reader#1 (555)",
            ))
        # User sees exactly the static friendly message — nothing else leaks.
        self.assertEqual(len(channel.sent), 1)
        content, kwargs = channel.sent[0]
        self.assertEqual(content, errors.FRIENDLY_ERROR_MESSAGE)
        for banned in ("boom", "SELECT", "sk-", "/opt/", "Traceback", "RuntimeError"):
            self.assertNotIn(banned, str(content))
        # Logs keep full technical detail plus context.
        self.assertIn("Traceback (most recent call last)", captured.text)
        self.assertIn("RuntimeError", captured.text)
        self.assertIn("action=nominate", captured.text)
        self.assertIn("guild=424242", captured.text)
        self.assertIn("user=Reader#1 (555)", captured.text)
        self.assertIn("surface=message", captured.text)

    def test_reply_channel_resolution_failure_still_never_raises(self):
        message = FakeMessage(FakeChannel())
        with mock.patch.object(errors, "get_reply_channel", side_effect=RuntimeError("resolve blew up")):
            asyncio.run(errors.handle_message_failure(message, make_secret_exception()))

    def test_send_failure_is_swallowed_and_logged(self):
        channel = FakeChannel(fail=True)
        message = FakeMessage(channel)
        with mock.patch.object(errors, "get_reply_channel", return_value=channel), LogCapture() as captured:
            asyncio.run(errors.handle_message_failure(message, make_secret_exception()))
        self.assertIn("Failed to deliver the fallback", captured.text)


class SlashCommandFailureTests(unittest.TestCase):
    def test_response_used_when_nothing_sent_yet(self):
        interaction = FakeInteraction(done=False)
        with LogCapture() as captured:
            asyncio.run(errors.handle_interaction_failure(interaction, make_secret_exception()))
        self.assertEqual(len(interaction.response.calls), 1)
        method, args, kwargs = interaction.response.calls[0]
        self.assertEqual(method, "send_message")
        self.assertEqual(args[0], errors.FRIENDLY_ERROR_MESSAGE)
        self.assertTrue(kwargs.get("ephemeral"))
        self.assertEqual(len(interaction.followup.calls), 0)
        # Logs carry command/guild/user context and the full traceback.
        self.assertIn("command=poll", captured.text)
        self.assertIn("guild=987", captured.text)
        self.assertIn("user=Reader#0001 (555)", captured.text)
        self.assertIn("surface=interaction", captured.text)
        self.assertIn("Traceback (most recent call last)", captured.text)

    def test_followup_used_after_defer(self):
        interaction = FakeInteraction(done=True)
        with LogCapture():
            asyncio.run(errors.handle_interaction_failure(
                interaction,
                discord.app_commands.CommandInvokeError(cast(Any, SimpleNamespace(name="poll")), make_secret_exception()),
            ))
        self.assertEqual(len(interaction.response.calls), 0)
        self.assertEqual(len(interaction.followup.calls), 1)
        method, args, kwargs = interaction.followup.calls[0]
        self.assertEqual(method, "followup.send")
        self.assertEqual(args[0], errors.FRIENDLY_ERROR_MESSAGE)
        self.assertTrue(kwargs.get("ephemeral"))

    def test_original_exception_is_logged_not_the_wrapper(self):
        interaction = FakeInteraction(done=True)
        with LogCapture() as captured:
            asyncio.run(errors.handle_interaction_failure(
                interaction,
                discord.app_commands.CommandInvokeError(cast(Any, SimpleNamespace(name="poll")), ValueError("db constraint xyz")),
            ))
        self.assertIn("ValueError", captured.text)
        self.assertIn("db constraint xyz", captured.text)

    def test_delivery_failure_never_propagates(self):
        interaction = FakeInteraction(done=True)
        async def explode(*args, **kwargs):
            raise RuntimeError("discord is down")
        interaction.followup.send = explode
        asyncio.run(errors.handle_interaction_failure(interaction, make_secret_exception()))

    def test_check_failure_is_expected_input_error_and_stays_quiet(self):
        interaction = FakeInteraction(done=False)
        with LogCapture() as captured:
            asyncio.run(errors.handle_interaction_failure(
                interaction, discord.app_commands.CheckFailure("not allowed"),
            ))
        self.assertEqual(len(interaction.response.calls), 0)
        self.assertEqual(len(interaction.followup.calls), 0)
        self.assertIn("rejected", captured.text.lower())

    def test_view_mixin_routes_on_error_through_central_path(self):
        class DummyView(errors.SafeUIMixin, discord.ui.View):
            pass

        interaction = FakeInteraction(done=False, command_name="button")
        dummy = DummyView()
        with LogCapture() as captured:
            asyncio.run(dummy.on_error(cast(Any, interaction), make_secret_exception(), None))
        self.assertEqual(len(interaction.response.calls), 1)
        self.assertIn("command=DummyView", captured.text)


class LlmFailureTests(unittest.TestCase):
    def tearDown(self):
        import llm
        llm.set_llm_error_handler(None)

    def test_get_intent_returns_friendly_fallback_on_api_failure(self):
        import llm
        with mock.patch.object(llm, "_get_client", side_effect=RuntimeError("connection reset")):
            intent = llm.get_intent("I finished Dune", {})
        self.assertEqual(intent["action"], "unknown")
        self.assertIn("trouble reaching the book brain", intent["response"])
        self.assertFalse(intent["flag_injection"])

    def test_get_intent_fallback_on_malformed_json(self):
        import llm
        with mock.patch.object(llm, "_call_llm", return_value="this is not json"):
            intent = llm.get_intent("hello", {})
        self.assertEqual(intent["action"], "unknown")

    def test_actionable_api_failure_triggers_registered_alert_handler(self):
        import llm
        seen = []

        def handler(failure):
            seen.append(failure)

        llm.set_llm_error_handler(handler)
        error = RuntimeError("401 invalid api key")
        setattr(error, "status_code", 401)
        # Patch one level under _call_llm so its real error-reporting path runs.
        with mock.patch.object(llm, "_get_client", side_effect=error):
            intent = llm.get_intent("hello", {})
        self.assertEqual(intent["action"], "unknown")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["category"], "authentication")

    def test_non_actionable_api_failure_does_not_alert_but_still_falls_back(self):
        import llm
        seen = []
        llm.set_llm_error_handler(lambda failure: seen.append(failure))
        with mock.patch.object(llm, "_get_client", side_effect=ValueError("server hiccup")):
            intent = llm.get_intent("hello", {})
        self.assertEqual(intent["action"], "unknown")
        self.assertEqual(seen, [])

    def test_alert_handler_crash_does_not_affect_caller(self):
        import llm

        def broken_handler(failure):
            raise RuntimeError("alert delivery exploded")

        llm.set_llm_error_handler(broken_handler)
        error = RuntimeError("401 invalid api key")
        setattr(error, "status_code", 401)
        with mock.patch.object(llm, "_get_client", side_effect=error):
            intent = llm.get_intent("hello", {})
        self.assertEqual(intent["action"], "unknown")

    def test_classify_llm_error_categories(self):
        import llm

        auth = RuntimeError("authentication failure")
        setattr(auth, "status_code", 401)
        self.assertEqual(cast(dict[str, Any], llm.classify_llm_error(auth))["category"], "authentication")

        billing = RuntimeError("insufficient_credit_balance remaining: 0")
        self.assertEqual(cast(dict[str, Any], llm.classify_llm_error(billing))["category"], "credit/billing")

        quota = RuntimeError("insufficient_quota for this org")
        self.assertEqual(cast(dict[str, Any], llm.classify_llm_error(quota))["category"], "quota")

        rate = RuntimeError("429 too many requests")
        setattr(rate, "status_code", 429)
        self.assertEqual(cast(dict[str, Any], llm.classify_llm_error(rate))["category"], "rate limit")

        self.assertIsNone(llm.classify_llm_error(ValueError("something else")))

    def test_llm_failure_alert_cooldown_suppresses_duplicates(self):
        import jobs
        alerts = []

        async def fake_notify(discord_client, content):
            alerts.append(content)

        failure = {"provider": "anthropic", "category": "authentication", "status_code": 401}
        other = {"provider": "anthropic", "category": "rate limit", "status_code": 429}
        with mock.patch.object(jobs, "notify_admin", fake_notify), \
             mock.patch.object(jobs, "_last_llm_alert_at", {}):
            asyncio.run(jobs.notify_llm_failure(failure))
            asyncio.run(jobs.notify_llm_failure(failure))  # inside cooldown window
            asyncio.run(jobs.notify_llm_failure(other))    # different category passes
        self.assertEqual(len(alerts), 2)
        self.assertIn("authentication", alerts[0])
        self.assertIn("rate limit", alerts[1])


class ScheduledJobIsolationTests(unittest.TestCase):
    def test_year_in_review_recovery_does_not_run_on_august_startup(self):
        import jobs

        guild = SimpleNamespace(id=111)
        runner = mock.AsyncMock()
        with mock.patch.object(jobs, "client", SimpleNamespace(guilds=[guild])), \
             mock.patch.object(jobs, "missed_year_in_review") as missed, \
             mock.patch.object(jobs, "run_year_in_review_for_guild", runner):
            asyncio.run(jobs.recover_missed_year_in_review(datetime(2026, 8, 25, 23, 21)))

        missed.assert_not_called()
        runner.assert_not_awaited()

    def test_year_in_review_recovery_runs_once_just_after_scheduled_time(self):
        import jobs

        guild = SimpleNamespace(id=111)
        runner = mock.AsyncMock()
        with mock.patch.object(jobs, "client", SimpleNamespace(guilds=[guild])), \
             mock.patch.object(jobs, "missed_year_in_review", return_value=True) as missed, \
             mock.patch.object(jobs, "run_year_in_review_for_guild", runner):
            asyncio.run(jobs.recover_missed_year_in_review(datetime(2026, 1, 1, 9, 5)))

        missed.assert_called_once_with("111", 2026)
        runner.assert_awaited_once_with(guild, 2026)

    def test_year_in_review_recovery_skips_guild_with_existing_event(self):
        import jobs

        guild = SimpleNamespace(id=111)
        runner = mock.AsyncMock()
        with mock.patch.object(jobs, "client", SimpleNamespace(guilds=[guild])), \
             mock.patch.object(jobs, "missed_year_in_review", return_value=False), \
             mock.patch.object(jobs, "run_year_in_review_for_guild", runner):
            asyncio.run(jobs.recover_missed_year_in_review(datetime(2026, 1, 1, 10, 0)))

        runner.assert_not_awaited()

    def test_repeated_ready_events_do_not_duplicate_year_in_review_recovery(self):
        import jobs

        guild = SimpleNamespace(id=111)
        completed = set()
        runs = []

        def missed(guild_id, year):
            return (guild_id, year) not in completed

        async def run_once(target_guild, year):
            key = (str(target_guild.id), year)
            runs.append(key)
            completed.add(key)

        with mock.patch.object(jobs, "client", SimpleNamespace(guilds=[guild])), \
             mock.patch.object(jobs, "missed_year_in_review", side_effect=missed), \
             mock.patch.object(jobs, "run_year_in_review_for_guild", side_effect=run_once):
            now = datetime(2026, 1, 1, 10, 0)
            asyncio.run(jobs.recover_missed_year_in_review(now))
            asyncio.run(jobs.recover_missed_year_in_review(now))

        self.assertEqual(runs, [("111", 2026)])

    def test_failed_coroutine_future_is_logged_not_raised(self):
        import jobs
        future = concurrent.futures.Future()
        future.set_exception(RuntimeError("job exploded"))
        with LogCapture() as captured:
            jobs._watch_future(future, "test_job")  # must not raise
        self.assertIn("job=test_job", captured.text)
        self.assertIn("surface=scheduled_job", captured.text)
        self.assertIn("RuntimeError", captured.text)

    def test_cancelled_future_is_ignored(self):
        import jobs
        future = concurrent.futures.Future()
        future.cancel()
        jobs._watch_future(future, "cancelled_job")  # must not raise

    def test_successful_future_logs_nothing(self):
        import jobs
        future = concurrent.futures.Future()
        future.set_result("ok")
        with LogCapture() as captured:
            jobs._watch_future(future, "happy_job")
        self.assertEqual(captured.formatted, [])

    def test_launch_with_dead_loop_is_isolated(self):
        import jobs
        async def never_runs():
            pass

        coro = never_runs()
        saved = jobs.client
        jobs.client = None
        try:
            jobs._launch(coro, "dead_loop_job")  # must not raise
        finally:
            jobs.client = saved

    def test_one_guilds_failure_does_not_stop_other_guilds(self):
        import jobs

        guild_a = SimpleNamespace(id=111)
        guild_b = SimpleNamespace(id=222)
        channel_a, channel_b = object(), object()

        async def fake_send_summary(channel, title, narrative, **kwargs):
            sent.append((channel, narrative))

        def fake_recap_side_effect(activity):
            if activity["guild"] == "111":
                raise RuntimeError("LLM exploded for guild 111")
            return f"recap for {activity['guild']}"

        sent = []
        fake_client = SimpleNamespace(guilds=[guild_a, guild_b])
        with mock.patch.object(jobs, "client", fake_client), \
             mock.patch.object(jobs, "get_weekly_activity", lambda gid: {"guild": gid}), \
             mock.patch.object(jobs, "get_guild_settings", lambda gid: __import__("guild_settings").default_settings()), \
             mock.patch.object(jobs, "llm") as fake_llm, \
             mock.patch.object(jobs, "resolve_text_channel",
                               lambda guild, name: channel_a if guild.id == 111 else channel_b), \
             mock.patch.object(jobs, "send_summary", fake_send_summary):
            fake_llm.get_recap.side_effect = fake_recap_side_effect
            asyncio.run(jobs.run_weekly_recap())  # must not raise

        self.assertEqual(sent, [(channel_b, "recap for 222")])

    def test_guild_failure_from_sending_is_also_isolated(self):
        import jobs

        guild_a = SimpleNamespace(id=111)
        guild_b = SimpleNamespace(id=222)
        calls = []

        async def fake_send_summary(channel, title, narrative, **kwargs):
            calls.append(narrative)
            if narrative == "a recap":
                raise RuntimeError("channel deleted mid-send")

        fake_client = SimpleNamespace(guilds=[guild_a, guild_b])
        with mock.patch.object(jobs, "client", fake_client), \
             mock.patch.object(jobs, "get_weekly_activity", lambda gid: {"guild": gid}), \
             mock.patch.object(jobs, "get_guild_settings", lambda gid: __import__("guild_settings").default_settings()), \
             mock.patch.object(jobs, "llm") as fake_llm, \
             mock.patch.object(jobs, "resolve_text_channel", lambda guild, name: object()), \
             mock.patch.object(jobs, "send_summary", fake_send_summary):
            fake_llm.get_recap.side_effect = (
                lambda activity: "a recap" if activity["guild"] == "111" else "b recap"
            )
            with LogCapture():
                asyncio.run(jobs.run_weekly_recap())

        self.assertEqual(calls, ["a recap", "b recap"])


    def test_nudge_group_lookup_failure_does_not_skip_later_guilds(self):
        import jobs

        guild_a = SimpleNamespace(id=111)
        guild_b = SimpleNamespace(id=222)
        lookups = []

        def get_groups(guild_id, *_args):
            lookups.append(guild_id)
            if guild_id == "111":
                raise RuntimeError("database unavailable")
            return []

        with mock.patch.object(jobs, "client", SimpleNamespace(guilds=[guild_a, guild_b])), \
             mock.patch.object(jobs, "get_guild_settings", lambda gid: __import__("guild_settings").default_settings()), \
             mock.patch.object(jobs, "get_inactivity_nudge_groups", get_groups), \
             LogCapture():
            asyncio.run(jobs.run_inactivity_nudges())

        self.assertEqual(lookups, ["111", "222"])

    def test_taste_profile_user_lookup_failure_does_not_skip_later_guilds(self):
        import jobs

        guild_a = SimpleNamespace(id=111)
        guild_b = SimpleNamespace(id=222)
        lookups = []
        captured = {}

        class FakeScheduler:
            def get_job(self, _job_id):
                return None

            def add_job(self, func, *_args, **_kwargs):
                captured["job_fn"] = func

        def get_users(guild_id):
            lookups.append(guild_id)
            if guild_id == "111":
                raise RuntimeError("database unavailable")
            return []

        def run_inline(coro, _label):
            asyncio.run(coro)

        with mock.patch.object(jobs, "client", SimpleNamespace(guilds=[guild_a, guild_b])), \
             mock.patch.object(jobs, "scheduler", FakeScheduler()), \
             mock.patch.object(jobs, "get_guild_settings", lambda gid: __import__("guild_settings").default_settings()), \
             mock.patch.object(jobs, "get_all_active_users", get_users), \
             mock.patch.object(jobs, "_launch", run_inline), \
             LogCapture():
            jobs.schedule_taste_profile_updates()
            captured["job_fn"]()

        self.assertEqual(lookups, ["111", "222"])

class BotWiringTests(unittest.TestCase):
    """Source-level guards (bot.py cannot be imported: it calls client.run)."""

    def test_tree_error_hook_is_installed(self):
        src = open("bot.py").read()
        self.assertIn("command_tree.on_error = _on_app_command_error", src)
        self.assertIn("errors.handle_interaction_failure", src)

    def test_message_path_routes_failures_through_errors_module(self):
        src = open("bot.py").read()
        self.assertIn("errors.handle_message_failure", src)
        self.assertNotIn("traceback.print_exc()", src)

    def test_jobs_no_longer_print_tracebacks(self):
        src = open("jobs.py").read()
        self.assertNotIn("traceback.print_exc()", src)
        self.assertIn("errors.log_exception", src)

    def test_on_ready_serializes_startup(self):
        src = open("bot.py").read()
        self.assertIn("async with ready_lock():", src)
        self.assertIn("await _on_ready_startup()", src)
        self.assertIn("def ready_lock()", open("jobs.py").read())

        async def check():
            import jobs
            lock_a = jobs.ready_lock()
            lock_b = jobs.ready_lock()
            self.assertIs(lock_a, lock_b)
            async with lock_a:
                self.assertTrue(lock_b.locked())

        asyncio.run(check())


if __name__ == "__main__":
    unittest.main()
