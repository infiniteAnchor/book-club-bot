"""Multi-action messages: parsing, dispatch planning, on_message loop, dry-run harness."""
import asyncio
import contextlib
import json
import os
import sys
from types import SimpleNamespace
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import bot
import llm
from handlers import describe_dispatch, plan_dispatch


def _item(action, response="ok", **args):
    return {"action": action, "args": args, "response": response}


def _payload(*items, flag=False):
    return json.dumps({"actions": list(items), "flag_injection": flag})


# ── llm.get_intents / validate_intents ───────────────────────────────────────

def test_multi_action_payload_keeps_order():
    raw = _payload(_item("finish_book", title="Dune"), _item("rate_book", title="Dune", score=9),
                   _item("start_book", title="Hyperion"))
    with mock.patch.object(llm, "_call_llm", return_value=raw):
        intents = llm.get_intents("finished Dune, 9/10, starting Hyperion", {})
    assert [i["action"] for i in intents] == ["finish_book", "rate_book", "start_book"]
    assert intents[1]["args"] == {"title": "Dune", "score": 9}
    assert all(i["flag_injection"] is False for i in intents)


def test_legacy_single_object_still_parses():
    raw = json.dumps({"action": "query", "args": {}, "response": "ok", "flag_injection": False})
    with mock.patch.object(llm, "_call_llm", return_value=raw):
        assert llm.get_intents("what's on my shelf", {}) == [
            {"action": "query", "args": {}, "response": "ok", "flag_injection": False}
        ]


def test_fenced_payload_parses():
    raw = "```json\n" + _payload(_item("streak")) + "\n```"
    with mock.patch.object(llm, "_call_llm", return_value=raw):
        assert [i["action"] for i in llm.get_intents("streak?", {})] == ["streak"]


def test_invalid_items_dropped_duplicates_collapsed_and_capped():
    intents = llm.validate_intents({"actions": [
        _item("not_an_action"),
        _item("add_to_wishlist", title="Dune"),
        _item("add_to_wishlist", title="Dune"),
        {"action": "streak", "args": [], "response": "ok"},
        _item("add_to_wishlist", title="Piranesi"),
        _item("streak"),
        _item("leaderboard"),
    ], "flag_injection": False})
    assert [(i["action"], i["args"]) for i in intents] == [
        ("add_to_wishlist", {"title": "Dune"}),
        ("add_to_wishlist", {"title": "Piranesi"}),
        ("streak", {}),
    ]
    assert len(intents) == llm.MAX_ACTIONS


def test_flag_injection_applies_to_every_item():
    intents = llm.validate_intents({"actions": [_item("streak"), _item("leaderboard")], "flag_injection": True})
    assert all(i["flag_injection"] for i in intents)


def test_nothing_valid_falls_back():
    for payload in (None, {"actions": [], "flag_injection": False}, {"actions": [_item("bogus")], "flag_injection": False},
                    {"actions": [_item("streak")]}, {"actions": "streak", "flag_injection": False}):
        assert llm.validate_intents(payload) == [llm.fallback_intent()]


def _focus(action, ordinal=0, **args):
    return {"focus_request": {"action": action, "args": args, "ordinal": ordinal}}


def test_get_intent_prefers_focus_request():
    raw = _payload(_item("update_progress", page=220), _item("query", "your shelf"))
    with mock.patch.object(llm, "_call_llm", return_value=raw):
        assert llm.get_intent("page 220, and show my shelf", _focus("query"))["action"] == "query"
        assert llm.get_intent("page 220, and show my shelf", {})["action"] == "update_progress"
        assert llm.get_intent("page 220, and show my shelf", _focus("streak"))["action"] == "update_progress"


def test_get_intent_focus_tells_repeated_actions_apart():
    raw = _payload(_item("rate_book", "Dune!", title="Dune", score=9),
                   _item("rate_book", "Hyperion!", title="Hyperion", score=7))
    message = "rate Dune 9 and Hyperion 7"
    with mock.patch.object(llm, "_call_llm", return_value=raw):
        # Exact args, then title (re-intent args can drift), then position.
        assert llm.get_intent(message, _focus("rate_book", 1, title="Hyperion", score=7))["response"] == "Hyperion!"
        assert llm.get_intent(message, _focus("rate_book", 0, title="  hyperion ", rating=7))["response"] == "Hyperion!"
        assert llm.get_intent(message, _focus("rate_book", 1, title="Hyperion Cantos"))["response"] == "Hyperion!"
        assert llm.get_intent(message, _focus("rate_book", 0, title="Hyperion Cantos"))["response"] == "Dune!"


def test_focus_request_carries_args_and_ordinal():
    planned = [_item("rate_book", title="Dune", score=9), _item("start_book", title="Hyperion"),
               _item("rate_book", title="Hyperion", score=7)]
    assert llm.focus_request(planned, 2) == {
        "action": "rate_book", "args": {"title": "Hyperion", "score": 7}, "ordinal": 1,
    }
    assert llm.focus_request(planned, 1)["ordinal"] == 0


# ── handlers.plan_dispatch ───────────────────────────────────────────────────

def test_plan_drops_silent_and_unknown_when_something_is_actionable():
    planned = plan_dispatch([_item("silent"), _item("unknown"), _item("streak"), _item("unknown")])
    assert [i["action"] for i in planned] == ["streak"]


def test_plan_keeps_one_unknown_when_nothing_actionable():
    planned = plan_dispatch([_item("unknown", "first"), _item("unknown", "second")])
    assert [i["response"] for i in planned] == ["first"]


def test_plan_all_silent_is_empty():
    assert plan_dispatch([_item("silent")]) == []


def test_describe_dispatch_reports_registry_flags():
    assert describe_dispatch(_item("open_poll")) == {
        "handler": "_handle_open_poll", "admin_only": True, "guest_blocked": False, "opt_in_exempt": True,
    }
    assert describe_dispatch(_item("rate_book"))["guest_blocked"] is True


# ── bot.on_message dispatch loop ─────────────────────────────────────────────

def _message(text="msg"):
    return SimpleNamespace(
        author=SimpleNamespace(id=1, display_name="Reader", bot=False), guild=None,
        content=text, channel=SimpleNamespace(typing=contextlib.nullcontext),
        add_reaction=mock.AsyncMock(), remove_reaction=mock.AsyncMock(),
    )


async def _dispatch(intents, handlers, *, tier="member", settings=None, tracking=True, contexts=None):
    notices = []
    contexts = iter(contexts or [])

    def context(author_id, guild_id):
        return next(contexts, {"books": []})

    async def run(function, text, user_context):
        return intents

    async def notice(channel, description, **kwargs):
        notices.append(description)

    message = _message()
    # Fresh limiter: bot._llm_limiter is module-global, so dispatches here would
    # otherwise spend author 1's budget for later test files.
    with mock.patch.object(bot, "_llm_limiter", bot.TokenBucketLimiter()), \
         mock.patch.object(bot, "get_single_participation_guild", return_value="guild"), \
         mock.patch.object(bot, "_is_enrolled", return_value=True), \
         mock.patch.object(bot, "get_reply_channel", side_effect=lambda msg: msg.channel), \
         mock.patch.object(bot, "build_user_context", side_effect=context), \
         mock.patch.object(bot, "get_guild_settings",
                           return_value=settings or {"member_opt_in_enabled": False}), \
         mock.patch.object(bot, "get_member_feature_preferences",
                           return_value={bot.TRACKING_FEATURE: tracking}), \
         mock.patch.object(bot, "get_access_tier", return_value=tier), \
         mock.patch.object(bot, "channel_setting_label", return_value="#welcome"), \
         mock.patch.object(bot, "run_llm", side_effect=run), \
         mock.patch.object(bot, "send_notice", side_effect=notice), \
         mock.patch.object(bot, "_ACTION_HANDLERS", handlers), \
         mock.patch.object(bot, "DEBUG_INTENT", False):
        await bot.on_message(message)
        await asyncio.sleep(0)  # let reaction tasks run
    return notices, message


def test_each_action_dispatched_in_order_with_its_own_context():
    calls = []

    def recorder(name):
        async def handler(ctx, args, intent, user_context):
            focus = user_context.get("focus_request") or {}
            calls.append((name, args, intent["response"], focus.get("action")))
            user_context["mutated_by"] = name
        return handler

    intents = [
        {**_item("finish_book", "done!", title="Dune"), "flag_injection": False},
        {**_item("rate_book", "9/10!", title="Dune", score=9), "flag_injection": False},
    ]
    notices, message = asyncio.run(_dispatch(
        intents, {"finish_book": recorder("finish"), "rate_book": recorder("rate")},
    ))
    assert calls == [
        ("finish", {"title": "Dune"}, "done!", "finish_book"),
        ("rate", {"title": "Dune", "score": 9}, "9/10!", "rate_book"),
    ]
    assert notices == []
    message.add_reaction.assert_any_await("✅")


def test_later_actions_get_rebuilt_context():
    seen = []

    async def handler(ctx, args, intent, user_context):
        seen.append(user_context["books"])

    intents = [
        {**_item("finish_book", "done!", title="Dune"), "flag_injection": False},
        {**_item("rate_book", "9/10!", title="Dune", score=9), "flag_injection": False},
    ]
    asyncio.run(_dispatch(
        intents, {"finish_book": handler, "rate_book": handler},
        contexts=[{"books": ["reading"]}, {"books": ["finished"]}],
    ))
    assert seen == [["reading"], ["finished"]]


def test_dropped_unknown_fragment_gets_notice_and_no_check():
    ran = []

    async def handler(ctx, args, intent, user_context):
        ran.append(intent["action"])

    intents = [
        {**_item("finish_book", "done!", title="Dune"), "flag_injection": False},
        {**_item("unknown", "huh?"), "flag_injection": False},
    ]
    notices, message = asyncio.run(_dispatch(intents, {"finish_book": handler}))
    assert ran == ["finish_book"]
    assert notices == [bot.PARTIAL_ACTION_MESSAGE]
    assert mock.call("✅") not in message.add_reaction.await_args_list


def test_partial_parse_still_focuses_the_runnable_action():
    seen = []

    async def handler(ctx, args, intent, user_context):
        seen.append(user_context.get("focus_request"))

    intents = [
        {**_item("unknown", "huh?"), "flag_injection": False},
        {**_item("rate_book", "9/10!", title="Dune", score=9), "flag_injection": False},
    ]
    notices, _ = asyncio.run(_dispatch(intents, {"rate_book": handler}))
    assert seen == [{"action": "rate_book", "args": {"title": "Dune", "score": 9}, "ordinal": 0}]
    assert notices == [bot.PARTIAL_ACTION_MESSAGE]


def test_repeated_rate_book_reintent_answers_its_own_book():
    import handlers

    sent = []
    reintent_raw = _payload(_item("rate_book", "Dune, nominated for the spice!", title="Dune", score=9),
                            _item("rate_book", "Hyperion, nominated for the Shrike!", title="Hyperion", score=7))

    async def send(ctx, intent):
        sent.append(intent["response"])

    intents = [
        {**_item("rate_book", "Dune 9", title="Dune", score=9), "flag_injection": False},
        {**_item("rate_book", "Hyperion 7", title="Hyperion", score=7), "flag_injection": False},
    ]
    with mock.patch.object(handlers, "run_book_action", mock.AsyncMock(return_value=True)), \
         mock.patch.object(handlers, "check_achievements", return_value=[]), \
         mock.patch.object(handlers, "get_nomination_context_for_book",
                           return_value={"nomination_reason": "a classic"}), \
         mock.patch.object(handlers, "_send_response", side_effect=send), \
         mock.patch.object(handlers, "DEBUG_INTENT", False), \
         mock.patch.object(llm, "_call_llm", return_value=reintent_raw):
        asyncio.run(_dispatch(intents, {"rate_book": handlers._handle_rate_book}))
    assert sent == ["Dune, nominated for the spice!", "Hyperion, nominated for the Shrike!"]


def test_single_action_gets_no_focus_request():
    seen = []

    async def handler(ctx, args, intent, user_context):
        seen.append(user_context)

    asyncio.run(_dispatch([{**_item("query"), "flag_injection": False}], {"query": handler}))
    assert seen == [{"books": []}]


def test_guest_block_is_per_action_and_notice_sent_once():
    ran = []

    async def handler(ctx, args, intent, user_context):
        ran.append(intent["action"])

    intents = [
        {**_item("query"), "flag_injection": False},
        {**_item("add_to_wishlist", title="Dune"), "flag_injection": False},
        {**_item("rate_book", title="Dune"), "flag_injection": False},
    ]
    notices, message = asyncio.run(_dispatch(
        intents, {"query": handler, "add_to_wishlist": handler, "rate_book": handler}, tier="guest",
    ))
    assert ran == ["query"]
    assert notices == [bot.GUEST_DENIED_MESSAGE]
    assert mock.call("✅") not in message.add_reaction.await_args_list


def test_opt_in_gate_still_allows_exempt_actions():
    ran = []

    async def handler(ctx, args, intent, user_context):
        ran.append(intent["action"])

    intents = [
        {**_item("help"), "flag_injection": False},
        {**_item("streak"), "flag_injection": False},
    ]
    notices, _ = asyncio.run(_dispatch(
        intents, {"help": handler, "streak": handler},
        settings={"member_opt_in_enabled": True, "opt_in_welcome_channel": ""}, tracking=False,
    ))
    assert ran == ["help"]
    assert len(notices) == 1 and "join the book club" in notices[0]


# ── dry-run harness ──────────────────────────────────────────────────────────

def test_dry_run_harness_writes_report(tmp_path):
    import dry_run_intents

    cases = dry_run_intents.load_cases(dry_run_intents.DEFAULT_INPUT)
    assert cases and all(isinstance(c["context"], dict) for c in cases)

    def fake_get_intents(message, context):
        if message.startswith("Add Piranesi"):
            return llm.validate_intents({"actions": [
                _item("add_to_wishlist", title="Piranesi"), _item("add_to_wishlist", title="The Hobbit"),
            ], "flag_injection": False})
        return [llm.fallback_intent()]

    records = dry_run_intents.dry_run(cases[:2], get_intents=fake_get_intents)
    out = tmp_path / "report.json"
    dry_run_intents.write_report(records, str(out))
    report = json.loads(out.read_text())
    assert report["summary"] == {"messages": 2, "checked": 2, "passed": 1, "multi_action": 1}
    wishlist = report["results"][1]
    assert [s["action"] for s in wishlist["planned"]] == ["add_to_wishlist", "add_to_wishlist"]
    assert wishlist["planned"][0]["handler"] == "_handle_simple"
    assert wishlist["pass"] is True


def test_dry_run_harness_reads_txt(tmp_path):
    import dry_run_intents

    path = tmp_path / "msgs.txt"
    path.write_text("# comment\nfinished Dune\n\nshow my streak\n")
    assert [c["message"] for c in dry_run_intents.load_cases(str(path))] == ["finished Dune", "show my streak"]
