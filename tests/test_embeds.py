import ast
import asyncio
import os
import unittest
from typing import Any, cast
from unittest import mock

import discord

from embeds import (
    COLORS,
    FOOTER,
    ITEMS_PER_PAGE,
    PagedEmbedView,
    build_release_board_embed,
    make_embed,
    make_intent_embed,
    make_list_embeds,
)
from sending import (
    ENROLLMENT_PANEL_TEXT,
    make_enrollment_embed,
    make_nudge_embed,
    make_shelf_picker_embed,
    role_broadcast_payload,
    send_nominations_summary,
    send_notice,
    send_poll_close,
)


class FakeChannel:
    def __init__(self):
        self.calls = []
        self.guild = object()

    async def send(self, content=None, **kwargs):
        self.calls.append((content, kwargs))


class EmbedFactoryTests(unittest.TestCase):
    def test_book_activity_uses_existing_response_and_facts(self):
        embed = make_intent_embed(
            "update_progress",
            "Nice work — you're moving through it.",
            {"title": "Dune", "author": "Frank Herbert", "progress_raw": 200, "progress_unit": "pages"},
        )
        data = cast(dict[str, Any], embed.to_dict())
        self.assertEqual(data["title"], "📍 Reading progress updated")
        self.assertEqual(data["description"], "Nice work — you're moving through it.")
        self.assertEqual(data["color"], COLORS["activity"])
        self.assertEqual({field["name"] for field in data["fields"]}, {"Book", "Author", "Progress"})

    def test_overlong_copy_is_safe_for_discord(self):
        embed = make_intent_embed("recommend", "x" * 5000, {})
        self.assertLessEqual(len(cast(str, embed.description)), 4096)
        self.assertTrue(cast(str, embed.description).endswith("…"))

    def test_lists_split_into_ten_item_pages(self):
        items = [{"title": f"Book {number}", "detail": "Reading"} for number in range(23)]
        pages = make_list_embeds("Shelf", items)
        self.assertEqual(len(pages), 3)
        self.assertEqual(len(pages[0].fields), ITEMS_PER_PAGE)
        self.assertEqual(len(pages[2].fields), 3)
        self.assertIn("Page 3/3", cast(str, pages[2].footer.text))

    def test_paginator_disables_first_previous_and_last_next(self):
        pages = make_list_embeds("Shelf", [{"title": str(i)} for i in range(11)])
        view = PagedEmbedView(pages, invoker_id=123)
        self.assertTrue(view.previous.disabled)
        self.assertFalse(view.next.disabled)
        view.index = 1
        view._sync_buttons()
        self.assertFalse(view.previous.disabled)
        self.assertTrue(view.next.disabled)

    def test_recommend_intent_embed_title_and_color(self):
        embed = make_intent_embed("recommend", "Try Piranesi next.", {})
        data = cast(dict[str, Any], embed.to_dict())
        self.assertEqual(data["title"], "🔎 Your next read")
        self.assertEqual(data["color"], COLORS["activity"])
        self.assertEqual(data["description"], "Try Piranesi next.")

    def test_release_board_embed_is_silent_info_card(self):
        embed = build_release_board_embed("**2026-09-02 — current club book**\n• Ask `/current`.")
        data = cast(dict[str, Any], embed.to_dict())
        self.assertEqual(data["title"], "📚 What's new")
        self.assertEqual(data["color"], COLORS["info"])
        self.assertIn("Ask `/current`.", data["description"])

    def test_make_embed_uses_palette_footer_and_trim(self):
        embed = make_embed("📚 Book Club", "x" * 5000, kind="info")
        data = cast(dict[str, Any], embed.to_dict())
        self.assertEqual(data["color"], COLORS["info"])
        self.assertEqual(data["footer"]["text"], FOOTER)
        self.assertLessEqual(len(data["description"]), 4096)
        self.assertTrue(data["description"].endswith("…"))

    def test_send_notice_sends_one_palette_embed(self):
        channel = FakeChannel()
        asyncio.run(send_notice(channel, "Nope.", title="🔥 Reading streak", kind="milestone"))
        self.assertEqual(len(channel.calls), 1)
        content, kwargs = channel.calls[0]
        self.assertIsNone(content)
        data = cast(dict[str, Any], kwargs["embed"].to_dict())
        self.assertEqual(data["title"], "🔥 Reading streak")
        self.assertEqual(data["description"], "Nope.")
        self.assertEqual(data["color"], COLORS["milestone"])
        self.assertEqual(data["footer"]["text"], FOOTER)

    def test_enrollment_embed_is_activity_without_mentions(self):
        embed = make_enrollment_embed()
        data = cast(dict[str, Any], embed.to_dict())
        self.assertEqual(data["title"], "👋 Join Book Club")
        self.assertEqual(data["color"], COLORS["activity"])
        self.assertEqual(data["description"], ENROLLMENT_PANEL_TEXT)
        self.assertNotIn("@", data["description"])
        self.assertNotIn("<@", data["description"])
        self.assertIn("**Tracking**", data["description"])
        self.assertIn("**Updates**", data["description"])
        self.assertIn("**Club**", data["description"])
        self.assertIn("**Chat only**", data["description"])
        self.assertIn("not part of Updates", data["description"])
        self.assertNotIn("inactivity nudges", data["description"])

    def test_nudge_embed_includes_book_titles(self):
        group = {
            "display_name": "Bob",
            "books": [
                {"title": "Dune", "author": "Frank Herbert", "progress_raw": 120, "progress_unit": "pages"},
                {"title": "Piranesi", "author": "Susanna Clarke"},
            ],
        }
        embed = make_nudge_embed(group)
        data = cast(dict[str, Any], embed.to_dict())
        self.assertEqual(data["color"], COLORS["activity"])
        self.assertIn("Dune", data["description"])
        self.assertIn("Piranesi", data["description"])
        self.assertEqual(data["footer"]["text"], FOOTER)

    def test_workflow_close_announcements_are_silent(self):
        channel = FakeChannel()
        asyncio.run(send_nominations_summary(channel, []))
        asyncio.run(send_poll_close(channel, {"winner": None}))
        self.assertEqual([content for content, _kwargs in channel.calls], [None, None])

    def test_shelf_picker_embed_uses_activity_not_discord_blue(self):
        embed = make_shelf_picker_embed(
            {"title": "Dune", "author": "Frank Herbert", "progress_raw": 50, "progress_unit": "pages"}
        )
        data = cast(dict[str, Any], embed.to_dict())
        self.assertEqual(data["title"], "📖 Dune")
        self.assertEqual(data["color"], COLORS["activity"])
        self.assertNotEqual(data["color"], 0x3498DB)
        self.assertEqual(data["footer"]["text"], FOOTER)
        self.assertIn("Frank Herbert", data["description"])
        self.assertIn("Use the buttons below", data["description"])
        self.assertEqual(data["fields"][0]["name"], "Progress")


class MemberFacingSendGuardTests(unittest.TestCase):
    """handlers.py and bot.py on_message must not send bare member-facing strings."""

    def test_handlers_and_bot_do_not_send_bare_member_strings(self):
        hits = _positional_string_sends("handlers.py") + _positional_string_sends("bot.py")
        self.assertEqual(hits, [], msg="bare .send('…') member replies:\n" + "\n".join(hits))

    def test_no_everyone_or_here_literals_in_member_facing_paths(self):
        hits = []
        for name in _MENTION_SCAN_FILES:
            for literal, node in _string_literals(name):
                if "@everyone" in literal or "@here" in literal:
                    hits.append(f"{name}:{node.lineno}: {literal[:80]}")
        self.assertEqual(
            hits,
            [],
            msg="broadcasts must resolve managed roles, never @everyone/@here:\n" + "\n".join(hits),
        )

    def test_content_sends_pass_explicit_allowed_mentions(self):
        hits = _content_sends_missing_allowed_mentions()
        self.assertEqual(
            hits,
            [],
            msg="member-facing content= sends must pass explicit allowed_mentions=:\n" + "\n".join(hits),
        )

    def test_views_shelf_picker_does_not_use_discord_blue(self):
        src = open("views_books.py").read()
        self.assertNotIn("discord.Color.blue()", src)
        self.assertIn("make_shelf_picker_embed", src)

    def test_send_guard_rejects_positional_variable_payloads(self):
        hits = _positional_sends_in_source(
            'async def reply(channel, payload):\n    await channel.send(payload)\n',
            "example.py",
        )
        self.assertEqual(hits, ["example.py:2: payload"])

def _positional_string_sends(path: str) -> list[str]:
    src = open(path).read()
    return _positional_sends_in_source(src, path)


def _positional_sends_in_source(src: str, path: str) -> list[str]:
    tree = ast.parse(src, filename=path)
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "send":
            continue
        if not node.args:
            continue
        first = node.args[0]
        snippet = ast.get_source_segment(src, first) or ""
        if "```json" in snippet:
            continue
        # Member-facing sends should use explicit content=/embed= keywords.
        # Reject every other positional payload, including a variable that may
        # contain plain text, so the guard cannot be bypassed by an assignment.
        hits.append(f"{path}:{node.lineno}: {snippet[:80]}")
    return hits



# Files whose user-visible output must never contain broad mention literals.
BROADCAST_SOURCE_FILES = (
    "sending.py",
    "jobs.py",
    "poll_threads.py",
    "poll_workflows.py",
    "embeds.py",
    "release_announcements.py",
)

_MENTION_SCAN_FILES = BROADCAST_SOURCE_FILES + (
    "handlers.py",
    "views.py",
    "views_books.py",
    "views_dashboard.py",
    "views_enrollment.py",
    "views_polls.py",
    "views_queue.py",
)


def _string_literals(name: str):
    """Yield (value, node) for string constants, skipping docstrings."""
    tree = ast.parse(open(name).read(), filename=name)
    docstrings = set()
    for scope in ast.walk(tree):
        if isinstance(scope, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = scope.body[0] if scope.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            yield node.value, node


def _content_sends_missing_allowed_mentions() -> list[str]:
    """Every content= send in broadcast files must pin explicit allowed_mentions=."""
    hits = []
    for name in BROADCAST_SOURCE_FILES:
        tree = ast.parse(open(name).read(), filename=name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = getattr(func, "attr", "") or (func.id if isinstance(func, ast.Name) else "")
            if not (isinstance(func, ast.Attribute) and func.attr in {"send", "edit"}) and called not in {"send_summary", "send_embeds"}:
                continue
            keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg}
            if "content" not in keywords:
                continue
            if isinstance(keywords["content"], ast.Constant) and keywords["content"].value is None:
                continue
            if "allowed_mentions" not in keywords:
                hits.append(f"{name}:{node.lineno}: {called}")
    return hits


class RoleBroadcastPayloadTests(unittest.TestCase):
    """role_broadcast_payload resolves managed roles with restricted mentions."""

    def _resolve(self, role):
        return mock.patch("sending.resolve", return_value=role)

    def test_ping_returns_role_mention_pinging_only_that_role(self):
        role = FakeRole(4242)
        with self._resolve(role):
            content, mentions = role_broadcast_payload(FakeGuild(), "club", ping=True)
        self.assertEqual(content, "<@&4242>")
        self.assertEqual(mentions.roles, [role])
        self.assertFalse(mentions.everyone)

    def test_silent_keeps_mention_visible_but_allows_nothing(self):
        role = FakeRole(4242)
        with self._resolve(role):
            content, mentions = role_broadcast_payload(FakeGuild(), "updates", ping=False)
        self.assertEqual(content, "<@&4242>")
        self.assertFalse(mentions.everyone)
        self.assertFalse(mentions.users)
        self.assertFalse(mentions.roles)
        self.assertFalse(mentions.replied_user)

    def test_missing_role_falls_back_to_empty_silent_payload(self):
        with self._resolve(None):
            content, mentions = role_broadcast_payload(None, "club", ping=True)
        self.assertEqual(content, "")
        self.assertFalse(mentions.everyone)
        self.assertFalse(mentions.users)
        self.assertFalse(mentions.roles)



class FakeRole:
    def __init__(self, role_id):
        self.id = role_id


class FakeGuild:
    pass


class BroadcastSendTests(unittest.TestCase):
    """Broadcast helpers forward the restricted payload to Discord."""

    def test_nominations_summary_is_silent(self):
        channel = FakeChannel()
        asyncio.run(send_nominations_summary(channel, []))
        content, kwargs = channel.calls[0]
        self.assertIsNone(content)
        self.assertIsNone(kwargs["allowed_mentions"])

    def test_poll_close_is_silent(self):
        channel = FakeChannel()
        asyncio.run(send_poll_close(channel, {"winner": None}))
        content, kwargs = channel.calls[0]
        self.assertIsNone(content)
        self.assertIsNone(kwargs["allowed_mentions"])


class GuestCopyTests(unittest.TestCase):
    def test_source_does_not_say_core_access_tier(self):
        for name in (
            "bot.py",
            "sending.py",
            "views_polls.py",
            "views_books.py",
            "views_queue.py",
            "views_enrollment.py",
            "commands.py",
            "help_content.py",
        ):
            text = open(name).read()
            self.assertNotIn("core access tier", text.lower(), name)


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    unittest.main()
