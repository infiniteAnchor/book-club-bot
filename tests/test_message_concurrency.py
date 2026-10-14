"""Chat context stays ordered for one reader while other readers can proceed."""
import asyncio
import contextlib
from types import SimpleNamespace
from unittest import mock

import bot


def test_same_reader_waits_for_updated_context_and_other_reader_proceeds():
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()
        other_finished = asyncio.Event()
        state = {"1": 0, "2": 0}
        contexts = []

        def context(user, guild):
            contexts.append((user, state[user]))
            return {"version": state[user]}

        async def run(function, message, user_context):
            if message == "first":
                entered.set()
                await release.wait()
            return [{"action": "query", "args": {}, "response": "ok"}]

        async def handler(ctx, args, intent, user_context):
            state[ctx.author_id] += 1
            if ctx.author_id == "2":
                other_finished.set()

        def message(user, text):
            return SimpleNamespace(
                author=SimpleNamespace(id=int(user), display_name="Reader", bot=False), guild=None,
                content=text, channel=SimpleNamespace(typing=contextlib.nullcontext), add_reaction=mock.AsyncMock(),
                remove_reaction=mock.AsyncMock(),
            )

        with mock.patch.object(bot, "get_single_participation_guild", return_value="guild"), \
             mock.patch.object(bot, "get_reply_channel", side_effect=lambda msg: msg.channel), \
             mock.patch.object(bot, "build_user_context", side_effect=context), \
             mock.patch.object(bot, "get_guild_settings", return_value={"member_opt_in_enabled": False}), \
             mock.patch.object(bot, "run_llm", side_effect=run), \
             mock.patch.object(bot, "_ACTION_HANDLERS", {"query": handler}), \
             mock.patch.object(bot, "DEBUG_INTENT", False):
            first = asyncio.create_task(bot.on_message(message("1", "first")))
            await asyncio.wait_for(entered.wait(), 1)
            second = asyncio.create_task(bot.on_message(message("1", "second")))
            other = asyncio.create_task(bot.on_message(message("2", "other")))
            try:
                await asyncio.wait_for(other_finished.wait(), 1)
                assert contexts == [("1", 0), ("2", 0)]
                assert not second.done()
            finally:
                release.set()
                await asyncio.gather(first, second, other)
            assert contexts[-1] == ("1", 1)
            assert state == {"1": 2, "2": 1}
            assert not bot._message_locks

    asyncio.run(scenario())


def test_guild_registration_writes_once_per_guild_name():
    with mock.patch.object(bot, "_registered_guilds", {}), \
         mock.patch.object(bot, "upsert_server") as upsert, \
         mock.patch.object(bot, "ensure_guild_settings") as ensure:
        bot._register_guild("g1", "Readers")
        bot._register_guild("g1", "Readers")
        bot._register_guild("g1", "Renamed Readers")
    assert [c.args for c in upsert.call_args_list] == [("g1", "Readers"), ("g1", "Renamed Readers")]
    assert ensure.call_count == 2
