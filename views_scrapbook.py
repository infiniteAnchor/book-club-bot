"""Opt-in, ephemeral scrapbook browsing: saved text can contain spoilers."""
from __future__ import annotations

import discord

import errors
from actions import get_access_tier
from actions_scrapbook import get_scrapbook, list_scrapbook_sessions
from actions_social import has_tracking
from embeds import PagedEmbedView, make_embed, make_list_embeds
from guild_settings import get_guild_settings


ADD_ERRORS = {
    "no_session": "No matching club read. Start one with /current, or choose a session from /scrapbook.",
    "invalid_session": "Use a valid session number from /scrapbook action:Sessions.",
    "invalid_content": "Provide 1–1000 characters of text to save.",
    "invalid_kind": "Choose quote, highlight, reaction, rating_note, or discussion.",
    "full": "This scrapbook has 100 entries. Remove one of your entries before adding another.",
}


def _text(value) -> str:
    return discord.utils.escape_markdown(discord.utils.escape_mentions(str(value)))


async def scrapbook_access_ok(interaction, guild_id: str, owner_id: int | None = None) -> bool:
    if not interaction.guild or str(interaction.guild.id) != guild_id:
        message = "Use this in your book-club server."
    elif owner_id is not None and interaction.user.id != owner_id:
        message = "Open your own scrapbook with /scrapbook."
    elif get_access_tier(str(interaction.user.id)) == "guest":
        message = "Join the book club with the enrollment panel before using the scrapbook."
    elif get_guild_settings(guild_id)["member_opt_in_enabled"] and not has_tracking(str(interaction.user.id), guild_id):
        message = "Join Tracking or a higher tier before using the scrapbook."
    else:
        return True
    await interaction.response.send_message(message, ephemeral=True)
    return False


def scrapbook_pages(guild_id: str, *, session_id=None, title=None, sessions=False):
    if sessions:
        items = [
            {"title": _text(row["title"]), "detail": f"Session #{row['session_id']} • {row['status']}"}
            for row in list_scrapbook_sessions(guild_id)
        ]
        pages = make_list_embeds("📔 Club scrapbooks", items, empty="No club reads yet. Start one with /current.")
        for page in pages:
            if items:
                page.description = "Use `/scrapbook session_id:` with a session number to open it. May contain spoilers."
        return pages
    data = get_scrapbook(guild_id, session_id=session_id, title=title)
    if data is None:
        return [make_embed("📔 Club scrapbook", ADD_ERRORS["no_session"])]
    session = data["session"]
    ratings = data["ratings"]
    rating_text = f"{ratings['average']}/10 ({ratings['count']} ratings)" if ratings["count"] else "No club ratings yet"
    description = (
        f"**{_text(session['title'])[:500]}**\n"
        f"Session #{session['session_id']} • {session['status']}\n"
        f"Started: {str(session['started_at'])[:10]}"
    )
    if session.get("completed_at"):
        description += f" • Completed: {str(session['completed_at'])[:10]}"
    description += (
        f"\n\n{data['finishes']} finished • {rating_text}\n"
        "Totals reflect club activity for this book across reads; Goodreads imports are excluded.\n\n"
        f"{len(data['entries'])} saved moments. Shared with club readers; may contain spoilers.\n"
        "Use `/scrapbook action:Add` to save a moment privately. "
        "Use `action:Remove entry_id:` to remove your own entry."
    )
    pages = [make_embed("📔 Club scrapbook", description, kind="milestone")]
    for entry in data["entries"]:
        page = make_embed(
            f"📔 {entry['kind'].replace('_', ' ').title()} #{entry['entry_id']}",
            _text(entry["content"]),
        )
        page.set_footer(text=f"Saved by {entry['display_name'][:100]} • Session #{session['session_id']}")
        pages.append(page)
    for index, page in enumerate(pages):
        footer = page.footer.text or "Club scrapbook"
        page.set_footer(text=f"{footer} • {index + 1}/{len(pages)}")
    return pages


class ScrapbookPages(errors.SafeUIMixin, PagedEmbedView):
    def __init__(self, pages, owner_id: int, guild_id: str):
        super().__init__(pages, invoker_id=owner_id)
        self.guild_id = guild_id

    async def _allow(self, interaction):
        return await scrapbook_access_ok(interaction, self.guild_id, self.invoker_id)


async def send_scrapbook(interaction, guild_id: str, **selection):
    pages = scrapbook_pages(guild_id, **selection)
    view = ScrapbookPages(pages, interaction.user.id, guild_id) if len(pages) > 1 else None
    await interaction.response.send_message(
        embed=pages[0], view=view, ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
    )
    if view:
        view.message = await interaction.original_response()


class RevealScrapbookView(errors.SafeUIMixin, discord.ui.View):
    def __init__(self, owner_id: int, guild_id: str, **selection):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.selection = selection

    @discord.ui.button(label="Open scrapbook (spoilers)", style=discord.ButtonStyle.primary)
    async def reveal(self, interaction, _button):
        if await scrapbook_access_ok(interaction, self.guild_id, self.owner_id):
            await send_scrapbook(interaction, self.guild_id, **self.selection)
