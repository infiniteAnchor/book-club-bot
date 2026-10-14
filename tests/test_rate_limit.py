"""Chat admission before the LLM: bot filtering, per-user budgets, alert truncation."""
import asyncio
import contextlib
from types import SimpleNamespace
from unittest import mock

import bot
from rate_limit import TokenBucketLimiter


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_bucket_allows_capacity_then_refills():
    clock = FakeClock()
    limiter = TokenBucketLimiter(clock=clock)
    assert [limiter.allow("k", 3, 60.0) for _ in range(4)] == [True, True, True, False]
    clock.now = 20.0  # one token back at 3 per 60s
    assert limiter.allow("k", 3, 60.0)
    assert not limiter.allow("k", 3, 60.0)


def test_buckets_are_per_key():
    limiter = TokenBucketLimiter(clock=FakeClock())
    assert limiter.allow("a", 1, 60.0)
    assert not limiter.allow("a", 1, 60.0)
    assert limiter.allow("b", 1, 60.0)


def test_notify_once_per_throttled_stretch():
    clock = FakeClock()
    limiter = TokenBucketLimiter(clock=clock)
    limiter.allow("k", 1, 60.0)
    assert not limiter.allow("k", 1, 60.0)
    assert limiter.should_notify("k")
    assert not limiter.should_notify("k")
    clock.now = 60.0
    assert limiter.allow("k", 1, 60.0)
    assert not limiter.allow("k", 1, 60.0)
    assert limiter.should_notify("k")


def test_prune_drops_idle_buckets():
    clock = FakeClock()
    limiter = TokenBucketLimiter(clock=clock)
    limiter.allow("old", 1, 60.0)
    clock.now = 4000.0
    limiter.allow("new", 1, 60.0)
    limiter.prune()
    assert list(limiter._buckets) == ["new"]


def _message(text="hello", is_bot=False, user=1):
    channel = SimpleNamespace(typing=contextlib.nullcontext)
    return SimpleNamespace(
        author=SimpleNamespace(id=user, display_name="Reader", bot=is_bot), guild=None,
        content=text, channel=channel, mentions=[],
        add_reaction=mock.AsyncMock(), remove_reaction=mock.AsyncMock(),
    )


async def _send(messages, *, enrolled=True, intent=None):
    """Send messages through on_message with a fresh limiter; return (llm calls, notices, alerts)."""
    llm_calls = []
    notices = []
    alerts = []
    intent = intent or {"action": "query", "args": {}, "response": "ok"}

    async def run(function, text, user_context):
        llm_calls.append(text)
        return [intent]

    async def notice(channel, description, **kwargs):
        notices.append(description)

    async def alert(client, content):
        alerts.append(content)

    with mock.patch.object(bot, "_llm_limiter", TokenBucketLimiter(clock=FakeClock())), \
         mock.patch.object(bot, "get_single_participation_guild", return_value="guild"), \
         mock.patch.object(bot, "get_reply_channel", side_effect=lambda msg: msg.channel), \
         mock.patch.object(bot, "build_user_context", return_value={}), \
         mock.patch.object(bot, "_is_enrolled", return_value=enrolled), \
         mock.patch.object(bot, "get_guild_settings", return_value={"member_opt_in_enabled": False}), \
         mock.patch.object(bot, "run_llm", side_effect=run), \
         mock.patch.object(bot, "send_notice", side_effect=notice), \
         mock.patch.object(bot, "notify_admin", side_effect=alert), \
         mock.patch.object(bot, "_ACTION_HANDLERS", {"query": mock.AsyncMock()}), \
         mock.patch.object(bot, "DEBUG_INTENT", False):
        for message in messages:
            await bot.on_message(message)
    return llm_calls, notices, alerts


def test_bot_and_webhook_messages_never_reach_llm():
    llm_calls, notices, _ = asyncio.run(_send([_message(is_bot=True)]))
    assert llm_calls == []
    assert notices == []


def test_member_budget_throttles_before_llm_and_notifies_once():
    messages = [_message(f"m{i}") for i in range(12)]
    llm_calls, notices, _ = asyncio.run(_send(messages))
    assert len(llm_calls) == 10
    assert notices == [bot.RATE_LIMITED_MESSAGE]


def test_unenrolled_members_get_smaller_budget():
    messages = [_message(f"m{i}") for i in range(5)]
    llm_calls, notices, _ = asyncio.run(_send(messages, enrolled=False))
    assert len(llm_calls) == 3
    assert notices == [bot.RATE_LIMITED_MESSAGE]


def test_injection_alert_truncates_message_text():
    text = "x" * 1000
    intent = {"action": "silent", "args": {}, "response": "", "flag_injection": True}
    _, _, alerts = asyncio.run(_send([_message(text)], intent=intent))
    assert len(alerts) == 1
    assert "x" * bot.INJECTION_ALERT_MAX_CHARS + "…" in alerts[0]
    assert "x" * (bot.INJECTION_ALERT_MAX_CHARS + 1) not in alerts[0]


def test_is_enrolled_true_when_opt_in_disabled():
    with mock.patch.object(bot, "get_guild_settings", return_value={"member_opt_in_enabled": False}), \
         mock.patch.object(bot, "get_member_feature_preferences") as prefs:
        assert bot._is_enrolled("1", "guild")
    prefs.assert_not_called()


def test_is_enrolled_follows_tracking_preference():
    settings = {"member_opt_in_enabled": True}
    with mock.patch.object(bot, "get_guild_settings", return_value=settings), \
         mock.patch.object(bot, "get_member_feature_preferences",
                           return_value={bot.TRACKING_FEATURE: False}):
        assert not bot._is_enrolled("1", "guild")
