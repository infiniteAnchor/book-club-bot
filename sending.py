"""Message-sending helpers and copy formatters shared by handlers, views, and jobs.

All output-side helpers live here so the Discord-facing layers never duplicate
embed-building or copy formatting. Pure formatting helpers are included next to
the send functions they historically paired with.
"""
from typing import Mapping

import discord

from actions_session import get_current_read
from embeds import (
    build_current_read_embed,
    make_achievement_embed,
    make_embed,
    make_list_embeds,
    make_poll_embed,
    make_summary_embed,
    send_embeds,
)
from participation_roles import resolve


INCOMPLETE_ACTION_MESSAGE = (
    "I couldn't complete that cleanly. Try rephrasing it with the book title and, "
    "if relevant, your progress or rating."
)
INCOMPLETE_ACTION_SHORT = "I couldn't complete that cleanly. Try rephrasing."
PARTIAL_ACTION_MESSAGE = "I handled the rest, but didn't catch part of that message. Try that part again on its own."

QUEUE_LIST_PUBLIC_ACK = (
    "Your private queue stays private. Use the button to view it here — only you will see the titles."
)

GUEST_DENIED_MESSAGE = (
    "Guests can't do that. Ask an admin to upgrade your access."
)
CLUB_REQUIRED_NOMINATE = (
    "Join the Book Club tier with the enrollment panel before nominating."
)
NOMINATION_NO_WINDOW = (
    "There's no nomination window open right now. Ask an admin to open one."
)

ENROLLMENT_PANEL_TITLE = "👋 Join Book Club"
ENROLLMENT_PANEL_TEXT = (
    "Pick your level \u2014 each tier includes everything below it.\n\n"
    "**Tracking** \u2014 log books, rates, and track your reading streak\n"
    "**Updates** \u2014 + weekly reading recaps in the club\n"
    "**Club** \u2014 + book nominations, polls, and voting\n"
    "**Chat only** \u2014 stay in server chat without those member features\n\n"
    "**DM nudges** are a separate optional add-on (needs Tracking), not part of Updates — "
    "use Toggle nudges.\n"
    "You can change this anytime with these buttons or by messaging me."
)


def nomination_denied_message(discord_id: str, guild_id: str | None) -> str | None:
    """Return guest/Club denial copy, or None when nominating is allowed."""
    from actions import get_access_tier
    from actions_social import has_club
    from guild_settings import get_guild_settings

    if get_access_tier(discord_id) == "guest":
        return GUEST_DENIED_MESSAGE
    if (
        guild_id
        and get_guild_settings(guild_id)["member_opt_in_enabled"]
        and not has_club(discord_id, guild_id)
    ):
        return CLUB_REQUIRED_NOMINATE
    return None


def make_nomination_embed(
    display_name: str,
    title: str,
    author: str | None,
    reason: str | None,
    *,
    replaced: bool = False,
):
    """Python-owned embed for a public nomination from slash or the Nominate button."""
    verb = "updated their nomination to" if replaced else "nominated"
    author_bit = f" by {author}" if author else ""
    description = f"**{display_name}** {verb} **{title}**{author_bit}."
    if reason:
        description += f"\n_{reason}_"
    return make_embed("🗳️ Nomination", description, kind="poll")


def role_broadcast_payload(guild, role_key: str, *, ping: bool) -> tuple[str, discord.AllowedMentions]:
    """Mention text and restricted AllowedMentions for a managed-role broadcast.

    Resolves the guild's managed `role_key` role. If roles are not bootstrapped
    yet (no stored ID), the payload is empty and fully silent — broadcasts never
    fall back to @everyone/@here.
    """
    role = resolve(guild, role_key)
    if role is None:
        return "", discord.AllowedMentions.none()
    mention = f"<@&{role.id}>"
    if ping:
        return mention, discord.AllowedMentions(roles=[role], everyone=False)
    return mention, discord.AllowedMentions.none()


async def send_notice(channel, description, *, title="📚 Book Club", kind="info"):
    """Send a single branded notice embed for member-facing guidance and denials."""
    return await send_embeds(channel, [make_embed(title, description, kind=kind)])


def make_enrollment_embed() -> discord.Embed:
    return make_embed(ENROLLMENT_PANEL_TITLE, ENROLLMENT_PANEL_TEXT, kind="activity")


def format_inactivity_nudge(group: dict) -> str:
    lines = [
        f"Hey {group['display_name']}, quick book club check-in.",
        "You’ve got a reading streak in progress and I haven’t seen an update in a while.",
        "",
        "Current reads:",
    ]
    for book in group["books"]:
        author_part = f" by {book['author']}" if book.get("author") else ""
        progress_part = ""
        if book.get("progress_raw") is not None:
            unit = book.get("progress_unit")
            if unit:
                progress_part = f" ({book['progress_raw']} {unit}"
                if book.get("progress_pct") is not None:
                    progress_part += f", {book['progress_pct']}%"
                progress_part += ")"
            else:
                progress_part = f" ({book['progress_raw']})"
        elif book.get("progress_pct") is not None:
            progress_part = f" ({book['progress_pct']}%)"
        lines.append(f"• **{book['title']}**{author_part}{progress_part}")
    lines.append("")
    lines.append("Reply here with an update, e.g. \"I’m on page 120 of Dune.\" If you finished one, I can log it too.")
    return "\n".join(lines)


def make_nudge_embed(group: dict) -> discord.Embed:
    return make_embed("📬 Reading check-in", format_inactivity_nudge(group), kind="activity")


def make_reminder_embed(reminder: Mapping) -> discord.Embed:
    embed = make_embed("⏰ Reminder", reminder.get("message") or "Time to read.", kind="info")
    book_title = reminder.get("book_title")
    if book_title:
        embed.add_field(name="Book", value=str(book_title), inline=False)
    return embed


def make_shelf_picker_embed(book: Mapping) -> discord.Embed:
    author = book.get("author") or "Unknown author"
    embed = make_embed(
        f"📖 {book.get('title', 'Book')}",
        f"{author}\n\nUse the buttons below to update this book.",
        kind="activity",
    )
    if book.get("progress_raw"):
        unit = book.get("progress_unit") or "pages"
        embed.add_field(name="Progress", value=f"{book['progress_raw']} {unit}", inline=True)
    return embed


async def send_summary(channel, title: str, narrative: str, *, kind: str = "info", content: str | None = None, allowed_mentions: discord.AllowedMentions | None = None):
    """Send a single narrative embed, retaining the existing copy verbatim."""
    return await send_embeds(channel, [make_summary_embed(title, narrative, kind=kind)], content=content, allowed_mentions=allowed_mentions)


async def send_nominations_summary(channel, noms: list):
    if not noms:
        return await send_summary(channel, "🗳️ Nominations closed", "No books were nominated this round.", kind="poll")
    items = [
        {
            "title": n.get("title"),
            "author": n.get("author"),
            "detail": f"Nominated by {n.get('display_name', 'a club member')}" +
                      (f" — {n['reason']}" if n.get("reason") else ""),
        }
        for n in noms
    ]
    return await send_embeds(
        channel,
        make_list_embeds("🗳️ Nominations closed", items, kind="poll"),
    )


async def send_current_read(channel, guild_id: str):
    """Post the Python-owned current-club-book embed."""
    return await send_embeds(channel, [build_current_read_embed(get_current_read(guild_id))])


async def send_poll_close(channel, result: dict, *, coordination: bool = False):
    """Present a completed book or coordination poll as an embed."""
    if coordination:
        if result.get("tied"):
            description = "The coordination poll ended in a tie. An admin must decide."
        elif result.get("winner"):
            winner = result["winner"]
            description = f"**{winner['label']}** wins with {winner['vote_count']} vote{'s' if winner['vote_count'] != 1 else ''}!"
        else:
            description = "No one voted in the coordination poll."
        items = result.get("all_results", [])
        title = "🗓️ Coordination poll closed"
    else:
        if result.get("tied"):
            description = "The poll ended in a tie."
            items = result.get("tied_nominations", [])
        elif result.get("winner"):
            winner = result["winner"]
            author = f" by {winner['author']}" if winner.get("author") else ""
            description = f"**{winner['title']}**{author} wins the vote! 🎉"
            items = [
                {**item["nomination"], "vote_count": item["vote_count"]}
                for item in result.get("all_results", [])
            ]
        else:
            description = "No one voted in this poll."
            items = []
        title = "🗳️ Poll closed"
    return await send_embeds(channel, [make_poll_embed(title, description, items)])


async def announce_achievements(channel, display_name: str, achievements: list):
    for ach in achievements:
        await send_embeds(channel, [make_achievement_embed(display_name, ach)])
