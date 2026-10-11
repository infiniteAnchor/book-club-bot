"""Discord presentation helpers for Book Club Bot.

This module deliberately contains no database or LLM work.  Python hands it
trusted facts plus the existing conversational copy, and it returns Discord
embeds that are safe to send from interactive and scheduled flows alike.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Mapping, Sequence

import discord
from typing import Any, cast


COLORS = {
    "success": 0x3F6B4F,       # forest
    "activity": 0x7A263A,      # burgundy
    "milestone": 0xB48A2C,     # gold
    "poll": 0x5865F2,          # Discord blurple
    "info": 0x6B7280,          # neutral gray
}
FOOTER = "Book Club Bot"
MAX_DESCRIPTION = 4_096
MAX_FIELD_VALUE = 1_024
ITEMS_PER_PAGE = 10


def _trim(value: object, limit: int) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _add_field(embed: discord.Embed, name: str, value: object, *, inline: bool = False) -> None:
    if value is not None and str(value).strip():
        embed.add_field(name=_trim(name, 256), value=_trim(value, MAX_FIELD_VALUE), inline=inline)


def make_embed(title: str, description: str = "", *, kind: str = "info") -> discord.Embed:
    embed = discord.Embed(
        title=_trim(title, 256),
        description=_trim(description, MAX_DESCRIPTION) or None,
        color=COLORS[kind],
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text=FOOTER)
    return embed


_ACTION_META = {
    "add_book": ("📚 Added to your shelf", "success"),
    "start_book": ("📖 Now reading", "activity"),
    "add_to_wishlist": ("✨ Added to your wishlist", "activity"),
    "remove_from_wishlist": ("✨ Wishlist updated", "activity"),
    "queue_add": ("🔒 Added to your private queue", "activity"),
    "queue_remove": ("🔒 Queue updated", "activity"),
    "queue_list": ("🔒 Your private queue", "activity"),
    "update_progress": ("📍 Reading progress updated", "activity"),
    "finish_book": ("🎉 Book finished", "success"),
    "rate_book": ("⭐ Rating logged", "milestone"),
    "dnf_book": ("🪦 Book laid to rest", "info"),
    "set_goal": ("🎯 Reading goal set", "milestone"),
    "set_reminder": ("⏰ Reminder set", "info"),
    "open_thread": ("🗣️ Spoiler thread", "activity"),
    "recommend": ("🔎 Your next read", "activity"),
    "query": ("📚 Your shelf", "info"),
    "group_query": ("👥 Club activity", "info"),
    "check_goal": ("🎯 Reading goal", "milestone"),
    "streak": ("🔥 Reading streak", "milestone"),
    "streak_leaderboard": ("🏆 Streak leaderboard", "milestone"),
    "leaderboard": ("🏆 Club leaderboard", "milestone"),
    "club_history": ("📚 Club history", "milestone"),
    "scrapbook": ("📔 Club scrapbook", "milestone"),
    "list_nominations": ("🗳️ Nominations", "poll"),
    "poll_results": ("🗳️ Poll results", "poll"),
    "coordination_poll_results": ("🗓️ Coordination poll", "poll"),
    "manage_membership": ("⚙️ Membership", "info"),
    "current_read": ("📖 Current club book", "activity"),
    "manage_club_read": ("📖 Club reading session", "activity"),
}


def make_intent_embed(action: str, response: str, args: Mapping | None = None) -> discord.Embed:
    """Format a normal natural-language response while retaining Claude copy."""
    title, kind = _ACTION_META.get(action, ("📚 Book Club", "info"))
    embed = make_embed(title, response, kind=kind)
    args = args or {}
    if args.get("title"):
        _add_field(embed, "Book", args["title"])
    if args.get("author"):
        _add_field(embed, "Author", args["author"])
    if args.get("progress_raw") is not None:
        unit = args.get("progress_unit") or ""
        _add_field(embed, "Progress", f"{args['progress_raw']} {unit}".strip(), inline=True)
    if args.get("score") is not None or args.get("rating") is not None:
        _add_field(embed, "Rating", f"{args.get('score', args.get('rating'))}/10", inline=True)
    if args.get("goal") is not None:
        _add_field(embed, "Goal", f"{args['goal']} books", inline=True)
    return embed


def make_achievement_embed(display_name: str, achievement: Mapping) -> discord.Embed:
    embed = make_embed("🏆 Achievement unlocked", achievement.get("description", ""), kind="milestone")
    _add_field(embed, "Reader", display_name, inline=True)
    _add_field(embed, "Achievement", achievement.get("title", ""), inline=True)
    return embed


def make_summary_embed(title: str, narrative: str, *, kind: str = "info") -> discord.Embed:
    return make_embed(title, narrative, kind=kind)


def build_release_board_embed(description: str) -> discord.Embed:
    """Python-owned persistent changelog embed. Never includes mentions."""
    from release_notes import RELEASE_BOARD_TITLE

    return make_embed(RELEASE_BOARD_TITLE, description, kind="info")


def _format_member_progress(member: Mapping) -> str:
    name = str(member.get("display_name") or "A member")
    status = member.get("status")
    if status == "finished":
        return f"{name} — finished"
    if status == "dnf":
        return f"{name} — DNF"
    pct = member.get("progress_pct")
    if pct is not None:
        return f"{name} — {pct}%"
    raw = member.get("progress_raw")
    if raw is not None:
        unit = member.get("progress_unit") or "pages"
        return f"{name} — {raw} {unit}"
    return f"{name} — reading"


def _format_started_at(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return "unknown"
    return text[:10]


def build_current_read_embed(session: Mapping | None) -> discord.Embed:
    """Python-owned current-club-book embed. `session` is already-fetched facts."""
    if not session:
        return make_embed(
            "📖 Current club book",
            "The club does not have a current book. An admin can start one with "
            "`/current start` or after a poll winner.",
            kind="info",
        )
    title = session.get("title") or "Untitled"
    embed = make_embed(f"📖 Currently reading: {title}", "", kind="activity")
    _add_field(embed, "Author", session.get("author"), inline=True)
    _add_field(embed, "Started", _format_started_at(session.get("started_at")), inline=True)
    if session.get("target_date"):
        _add_field(embed, "Target", session.get("target_date"), inline=True)
    members = list(session.get("members") or [])
    if not members:
        _add_field(
            embed,
            "Members",
            f"Nobody has logged this book yet. Say `I started {title}` to join in.",
        )
        return embed
    lines = [_format_member_progress(member) for member in members]
    shown: list[str] = []
    omitted = 0
    used = 0
    for line in lines:
        extra = len(line) + (1 if shown else 0)
        if used + extra > MAX_FIELD_VALUE:
            omitted = len(lines) - len(shown)
            break
        shown.append(line)
        used += extra
    body = "\n".join(shown)
    if omitted:
        suffix = f"\nand {omitted} more"
        if len(body) + len(suffix) > MAX_FIELD_VALUE:
            body = _trim(body, MAX_FIELD_VALUE - len(suffix))
        body = f"{body}{suffix}"
    _add_field(embed, "Members", body)
    return embed


def make_poll_embed(title: str, description: str, items: Iterable[Mapping] = (), *, kind: str = "poll") -> discord.Embed:
    embed = make_embed(title, description, kind=kind)
    # Poll buttons support at most 25 options; Discord embeds have the same
    # field cap, so keep every option visible alongside its button.
    for item in list(items)[:25]:
        label = item.get("title") or item.get("label") or "Option"
        author = item.get("author")
        votes = item.get("vote_count")
        name = f"{label}" + (f" — {author}" if author else "")
        value = f"{votes} vote{'s' if votes != 1 else ''}" if votes is not None else item.get("reason") or ""
        _add_field(embed, name, value or "—", inline=False)
    return embed


def make_list_embeds(title: str, items: Sequence[Mapping], *, kind: str = "info", empty: str = "Nothing to show yet.") -> list[discord.Embed]:
    """Return embeds with a maximum of ten list entries on each page."""
    if not items:
        return [make_embed(title, empty, kind=kind)]
    pages = []
    total = (len(items) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
    for start in range(0, len(items), ITEMS_PER_PAGE):
        page = start // ITEMS_PER_PAGE + 1
        embed = make_embed(title, "", kind=kind)
        for item in items[start : start + ITEMS_PER_PAGE]:
            label = item.get("title") or item.get("label") or item.get("display_name") or item.get("name") or "Item"
            author = item.get("author")
            value = item.get("detail") or item.get("reason") or item.get("status") or "—"
            if item.get("vote_count") is not None:
                value = f"{item['vote_count']} vote{'s' if item['vote_count'] != 1 else ''}"
            _add_field(embed, f"{label}" + (f" — {author}" if author else ""), value)
        embed.set_footer(text=f"{FOOTER} • Page {page}/{total}")
        pages.append(embed)
    return pages


class PagedEmbedView(discord.ui.View):
    """Short-lived paginator for member-invoked long lists."""

    def __init__(self, pages: Sequence[discord.Embed], invoker_id: int | None = None):
        super().__init__(timeout=900)
        self.pages = list(pages)
        self.invoker_id = invoker_id
        self.index = 0
        self.message: discord.Message | None = None
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        self.previous.disabled = self.index == 0
        self.next.disabled = self.index >= len(self.pages) - 1

    async def _allow(self, interaction: discord.Interaction) -> bool:
        if self.invoker_id is None or interaction.user.id == self.invoker_id:
            return True
        await interaction.response.send_message("Only the member who requested this list can change pages.", ephemeral=True)
        return False

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not await self._allow(interaction):
            return
        self.index -= 1
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not await self._allow(interaction):
            return
        self.index += 1
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    async def on_timeout(self) -> None:
        for child in self.children:
            cast(Any, child).disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


async def send_embeds(channel, pages: Sequence[discord.Embed], *, content: str | None = None, invoker_id: int | None = None, view=None, allowed_mentions: discord.AllowedMentions | None = None):
    """Send one embed or a paginated list; returns the sent Discord message."""
    if not pages:
        raise ValueError("send_embeds requires at least one embed")
    if len(pages) == 1:
        return await channel.send(content=content, embed=pages[0], view=view, allowed_mentions=allowed_mentions)
    paginator = PagedEmbedView(pages, invoker_id=invoker_id)
    message = await channel.send(content=content, embed=pages[0], view=paginator, allowed_mentions=allowed_mentions)
    paginator.message = message
    return message
