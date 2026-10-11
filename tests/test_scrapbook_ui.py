"""Scrapbook command, spoiler boundaries, and interaction access checks."""
import asyncio
import json
from unittest import mock

import discord
import pytest

import commands
import database
import handlers
import llm
import views_scrapbook
from actions_scrapbook import add_scrapbook_entry, get_scrapbook
from actions_session import start_club_read


@pytest.fixture
def scrapbook(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "club.db"))
    database.init_db()
    monkeypatch.setattr(views_scrapbook, "get_access_tier", lambda _: "core")
    monkeypatch.setattr(views_scrapbook, "get_guild_settings", lambda _: {"member_opt_in_enabled": True})
    monkeypatch.setattr(views_scrapbook, "has_tracking", lambda *_: True)
    return start_club_read("10", "1", {"title": "Dune"})[2]


def interaction(user_id=1, guild_id=10):
    result = mock.Mock()
    result.user.id = user_id
    result.user.display_name = "Reader"
    result.guild = mock.Mock(id=guild_id) if guild_id else None
    result.response.send_message = mock.AsyncMock()
    result.response.edit_message = mock.AsyncMock()
    result.original_response = mock.AsyncMock()
    return result


def test_real_discord_registration_accepts_scrapbook_options():
    client = discord.Client(intents=discord.Intents.none())
    tree = discord.app_commands.CommandTree(client)
    commands.register(tree)
    command = tree.get_command("scrapbook")
    assert command is not None
    assert {p.name for p in command.parameters} == {"action", "content", "kind", "session_id", "title", "entry_id"}
    assert all(len(p.description) <= 100 for p in command.parameters)


def test_add_browse_and_remove_are_ephemeral(scrapbook):
    async def run():
        add = interaction()
        await commands.slash_scrapbook(add, action=mock.Mock(value="add"), content="The hidden ending", kind=mock.Mock(value="quote"))
        assert add.response.send_message.call_args.kwargs["ephemeral"]
        assert "hidden ending" not in str(add.response.send_message.call_args)
        data = get_scrapbook("10")
        assert data["entries"][0]["content"] == "The hidden ending"
        browse = interaction()
        await commands.slash_scrapbook(browse)
        payload = browse.response.send_message.call_args.kwargs
        assert payload["ephemeral"]
        assert "hidden ending" in payload["view"].pages[1].description
        assert not payload["allowed_mentions"].everyone
        denied = interaction(user_id=2)
        await commands.slash_scrapbook(denied, action=mock.Mock(value="remove"), entry_id=data["entries"][0]["entry_id"])
        assert len(get_scrapbook("10")["entries"]) == 1
        remove = interaction()
        await commands.slash_scrapbook(remove, action=mock.Mock(value="remove"), entry_id=data["entries"][0]["entry_id"])
        assert get_scrapbook("10")["entries"] == []
    asyncio.run(run())


@pytest.mark.parametrize("reason", ["dm", "guest", "opted_out"])
def test_command_denies_before_writing(scrapbook, monkeypatch, reason):
    if reason == "guest":
        monkeypatch.setattr(views_scrapbook, "get_access_tier", lambda _: "guest")
    elif reason == "opted_out":
        monkeypatch.setattr(views_scrapbook, "has_tracking", lambda *_: False)
    request = interaction(guild_id=None if reason == "dm" else 10)
    asyncio.run(commands.slash_scrapbook(request, action=mock.Mock(value="add"), content="A note"))
    assert not get_scrapbook("10")["entries"]
    assert request.response.send_message.call_args.kwargs["ephemeral"]


def test_reveal_and_pages_recheck_owner_guild_and_membership(scrapbook, monkeypatch):
    async def run():
        add_scrapbook_entry("10", "1", "Reader", {"content": "Spoiler"})
        view = views_scrapbook.RevealScrapbookView(1, "10", session_id=scrapbook["session_id"])
        for request in (interaction(user_id=2), interaction(guild_id=20)):
            await view.reveal.callback(request)
            assert "embed" not in request.response.send_message.call_args.kwargs
        pages = views_scrapbook.ScrapbookPages(views_scrapbook.scrapbook_pages("10"), 1, "10")
        monkeypatch.setattr(views_scrapbook, "has_tracking", lambda *_: False)
        request = interaction()
        await view.reveal.callback(request)
        assert "embed" not in request.response.send_message.call_args.kwargs
        await pages.next.callback(request)
        request.response.edit_message.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize("debug", [True, False])
def test_chat_never_echoes_model_response_or_args(scrapbook, monkeypatch, debug):
    monkeypatch.setattr(handlers, "CONVERSATIONAL_RESPONSES", not debug)
    message = mock.Mock(guild=mock.Mock(id=10))
    channel = mock.Mock(send=mock.AsyncMock())
    ctx = handlers.RequestContext(message, "10", "1", "Reader", channel)
    intent = {"action": "scrapbook_add", "response": "SPOILER from the model", "args": {"content": "SPOILER in the entry"}}
    async def run():
        await handlers._handle_scrapbook_add(ctx, intent["args"], intent, {})
        await handlers._handle_scrapbook(ctx, {}, intent, {})
    asyncio.run(run())
    for call in channel.send.call_args_list:
        payload = str(call.args) + json.dumps(call.kwargs["embed"].to_dict())
        assert "SPOILER" not in payload


def test_pages_preserve_full_text_and_fit_discord_limits(scrapbook):
    content = "*" * 990 + "@everyone"
    add_scrapbook_entry("10", "1", "Reader", {"content": content})
    pages = views_scrapbook.scrapbook_pages("10")
    assert len(pages) == 2
    assert pages[1].description.count("*") == 990
    assert "@everyone" not in pages[1].description
    assert all(len(page.description) <= 4096 and len(page) <= 6000 for page in pages)
    archive = views_scrapbook.scrapbook_pages("10", sessions=True)
    assert f"Session #{scrapbook['session_id']}" in str(archive[0].to_dict())
    assert "No club reads yet" in views_scrapbook.scrapbook_pages("20", sessions=True)[0].description


@pytest.mark.parametrize("action,args", [
    ("scrapbook", {"title": "Dune"}),
    ("scrapbook_add", {"content": "A quote", "kind": "quote"}),
    ("scrapbook_remove", {"entry_id": 12}),
])
def test_synthetic_intents_parse(action, args):
    # Parser plumbing only; this does not claim live-provider classification.
    payload = {"action": action, "args": args, "response": "Understood.", "flag_injection": False}
    with mock.patch.object(llm, "_call_llm", return_value=json.dumps(payload)):
        assert llm.get_intent("Scrapbook request", {}) == payload
