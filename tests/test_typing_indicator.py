"""Intent LLM calls run inside the reply channel's typing indicator."""
import asyncio
import contextlib
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

import discord

import bot
import handlers


class TypingChannel:
    def __init__(self, fail=False):
        self.active = False
        self.entered = 0
        self.fail = fail

    @contextlib.asynccontextmanager
    async def typing(self):
        if self.fail:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Access")
        self.active = True
        self.entered += 1
        try:
            yield
        finally:
            self.active = False


async def _send_chat(channel, handler):
    """Run one chat message through on_message; return typing state per intent call."""
    seen = []

    async def run(function, message, user_context):
        seen.append(channel.active)
        return {"action": "query", "args": {}, "response": "ok"}

    message = SimpleNamespace(
        author=SimpleNamespace(id=1, display_name="Reader", bot=False), guild=None,
        content="what am I reading?", channel=channel,
        add_reaction=mock.AsyncMock(), remove_reaction=mock.AsyncMock(),
    )
    with mock.patch.object(bot, "get_single_participation_guild", return_value="guild"), \
         mock.patch.object(bot, "get_reply_channel", side_effect=lambda msg: msg.channel), \
         mock.patch.object(bot, "build_user_context", return_value={}), \
         mock.patch.object(bot, "get_guild_settings", return_value={"member_opt_in_enabled": False}), \
         mock.patch.object(bot, "run_llm", side_effect=run), \
         mock.patch.object(bot, "_ACTION_HANDLERS", {"query": handler}), \
         mock.patch.object(bot, "DEBUG_INTENT", False):
        await bot.on_message(message)
    return seen


def test_initial_intent_call_shows_typing():
    async def scenario():
        channel = TypingChannel()
        handler = mock.AsyncMock()
        assert await _send_chat(channel, handler) == [True]
        assert channel.entered == 1
        assert not channel.active
        handler.assert_awaited_once()

    asyncio.run(scenario())


def test_typing_failure_still_parses_intent():
    async def scenario():
        channel = TypingChannel(fail=True)
        handler = mock.AsyncMock()
        assert await _send_chat(channel, handler) == [False]
        handler.assert_awaited_once()

    asyncio.run(scenario())


def test_reintent_call_shows_typing():
    async def scenario():
        channel = TypingChannel()
        seen = []

        async def run(function, message, user_context):
            seen.append(channel.active)
            return {"response": "Goal set!"}

        ctx = handlers.RequestContext(
            message=cast(Any, SimpleNamespace(content="set my goal to 20 books")),
            guild_id="guild", author_id="1", display_name="Reader", reply_channel=channel,
        )
        with mock.patch.object(handlers, "action_set_goal", return_value=True), \
             mock.patch.object(handlers, "get_goal_progress", return_value={}), \
             mock.patch.object(handlers, "run_llm", side_effect=run), \
             mock.patch.object(handlers, "_send_response", mock.AsyncMock()) as send, \
             mock.patch.object(handlers, "DEBUG_INTENT", False):
            await handlers._handle_set_goal(ctx, {"goal": 20}, {}, {})

        assert seen == [True]
        assert not channel.active
        send.assert_awaited_once()

    asyncio.run(scenario())
