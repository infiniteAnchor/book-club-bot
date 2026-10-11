"""Reading-shelf and book-action Views/Modals."""
import re

import discord

import errors
from actions import (
    action_dnf_book,
    action_finish_book,
    action_rate_book,
    action_update_progress,
    get_access_tier,
    get_user_book_progress,
    run_book_action,
)
from actions_queue import list_next_book_choices
from actions_social import check_achievements, has_tracking
from guild_settings import get_guild_settings
from sending import GUEST_DENIED_MESSAGE, announce_achievements, make_shelf_picker_embed
from views_queue import OfferNextBookView, send_next_book_picker


class ProgressUpdateModal(errors.SafeUIMixin, discord.ui.Modal):
    """Collect a compact, deterministic progress update from a book-action button."""
    def __init__(self, book_actions):
        super().__init__(title=f"Update progress: {book_actions.title[:27]}")
        self.book_actions = book_actions
        self.progress = discord.ui.TextInput(label="Progress", placeholder="e.g. 120 pages or 35%", max_length=30)
        self.note = discord.ui.TextInput(label="Note (optional)", required=False, style=discord.TextStyle.paragraph, max_length=500)
        self.add_item(self.progress)
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction):
        match = re.fullmatch(r"\s*(\d+)\s*(%|percent|percentage|page|pages)?\s*", self.progress.value, re.IGNORECASE)
        if not match:
            await interaction.response.send_message("Use a number with `pages` or `%`, such as `120 pages` or `35%`.", ephemeral=True)
            return
        raw, unit = match.groups()
        unit = (unit or "pages").lower()
        if unit in ("%", "percent", "percentage"):
            unit = "percent"
        else:
            unit = "pages"
        await interaction.response.defer()
        ok = await run_book_action(action_update_progress,
            str(interaction.user.id), str(interaction.user.display_name), self.book_actions.guild_id,
            {"title": self.book_actions.title, "author": self.book_actions.author,
             "progress_raw": int(raw), "progress_unit": unit, "note": self.note.value.strip() or None},
        )
        if not ok:
            await interaction.followup.send("That book is not currently marked as reading.", ephemeral=True)
            return
        confirmation = f"Updated **{self.book_actions.title}** to **{raw} {unit}**."
        await self.book_actions._revert_to_shelf(interaction, confirmation)


class RatingModal(errors.SafeUIMixin, discord.ui.Modal):
    """Collect a 1–10 rating after a book has been finished."""
    def __init__(self, book_actions):
        super().__init__(title=f"Rate: {book_actions.title[:38]}")
        self.book_actions = book_actions
        self.score = discord.ui.TextInput(label="Rating (1–10)", placeholder="8", max_length=2)
        self.note = discord.ui.TextInput(label="Review note (optional)", required=False, style=discord.TextStyle.paragraph, max_length=1000)
        self.add_item(self.score)
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            score = int(self.score.value.strip())
        except ValueError:
            score = 0
        if not 1 <= score <= 10:
            await interaction.response.send_message("Choose a whole-number rating from 1 to 10.", ephemeral=True)
            return
        await interaction.response.defer()
        ok = await run_book_action(action_rate_book,
            str(interaction.user.id), str(interaction.user.display_name),
            {"title": self.book_actions.title, "author": self.book_actions.author,
             "score": score, "note": self.note.value.strip() or None},
            self.book_actions.guild_id,
        )
        if not ok:
            await interaction.followup.send("I couldn't save that rating. Try again.", ephemeral=True)
            return
        confirmation = f"Rated **{self.book_actions.title}** **{score}/10**."
        await self.book_actions._revert_to_shelf(interaction, confirmation)
        achievements = check_achievements(str(interaction.user.id), str(interaction.user.display_name), self.book_actions.guild_id)
        if achievements and interaction.channel:
            await announce_achievements(interaction.channel, str(interaction.user.display_name), achievements)


class ReadingListView(errors.SafeUIMixin, discord.ui.View):
    """Dropdown to pick a book from the reading list, then show action buttons."""
    def __init__(self, owner_id: int | str, guild_id: str, books: list):
        super().__init__(timeout=300)
        self.owner_id = int(owner_id)
        self.guild_id = guild_id
        self.books = books
        self.message: discord.Message | None = None  # Set after sending to enable edit_message
        self.original_embed: discord.Embed | None = None  # Store the shelf embed for revert
        options = []
        for book in books[:25]:  # Discord select max 25 options
            label = book["title"][:100]
            detail = book.get("author") or ""
            if book.get("progress_raw"):
                detail += f" — {book['progress_raw']} {book.get('progress_unit', 'pages')}"
            options.append(discord.SelectOption(label=label, description=detail[:100], value=str(len(options))))
        select = discord.ui.Select(placeholder="Pick a book to act on…", options=options)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Only you can use this.", ephemeral=True)
            return
        values = interaction.data.get("values", []) if interaction.data else []
        if not values:
            return
        idx = int(values[0])
        book = self.books[idx]
        view = BookActionView(
            self.owner_id, self.guild_id, book["title"], book.get("author"),
            can_progress=True, can_finish=True, can_dnf=True,
            shelf_view=self,  # Pass reference for revert
        )
        await interaction.response.edit_message(embed=make_shelf_picker_embed(book), view=view)


class BookActionView(errors.SafeUIMixin, discord.ui.View):
    """Author-only shortcuts for the book named in a successful tracking reply."""
    def __init__(
        self,
        owner_id: int | str,
        guild_id: str,
        title: str,
        author: str | None = None,
        *,
        can_progress: bool = False,
        can_finish: bool = False,
        can_rate: bool = False,
        can_dnf: bool = False,
        offer_next: bool = False,
        shelf_view=None,
    ):
        super().__init__(timeout=900)
        self.owner_id = int(owner_id)
        self.guild_id = guild_id
        self.title = title
        self.author = author
        self.shelf_view = shelf_view  # Reference to ReadingListView for revert
        if can_progress:
            button = discord.ui.Button(label="Update progress", style=discord.ButtonStyle.primary)
            button.callback = self._update_progress
            self.add_item(button)
        if can_finish:
            button = discord.ui.Button(label="Finish", style=discord.ButtonStyle.success)
            button.callback = self._finish
            self.add_item(button)
        if can_rate:
            button = discord.ui.Button(label="Rate", style=discord.ButtonStyle.primary)
            button.callback = self._rate
            self.add_item(button)
        if can_dnf:
            button = discord.ui.Button(label="Quit", style=discord.ButtonStyle.danger)
            button.callback = self._dnf
            self.add_item(button)
        if offer_next:
            next_btn = discord.ui.Button(
                label="Pick next from your lists", style=discord.ButtonStyle.success
            )
            next_btn.callback = self._offer_next
            self.add_item(next_btn)
        if self.shelf_view is not None:
            done = discord.ui.Button(label="Done", style=discord.ButtonStyle.secondary)
            done.callback = self._done
            self.add_item(done)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Only the reader who logged this book can use these buttons.", ephemeral=True)
            return False
        if get_access_tier(str(interaction.user.id)) == "guest":
            await interaction.response.send_message(GUEST_DENIED_MESSAGE, ephemeral=True)
            return False
        if get_guild_settings(self.guild_id)["member_opt_in_enabled"]:
            if not has_tracking(str(interaction.user.id), self.guild_id):
                await interaction.response.send_message("Join the book club before using book actions.", ephemeral=True)
                return False
        return True

    async def _revert_to_shelf(self, interaction: discord.Interaction, confirmation: str | None = None):
        """Edit the message back to the reading shelf embed after an action."""
        if not self.shelf_view or not self.shelf_view.original_embed:
            # Fallback: just send ephemeral confirmation
            if interaction.response.is_done():
                await interaction.followup.send(confirmation or "Done!", ephemeral=True)
            else:
                await interaction.response.send_message(confirmation or "Done!", ephemeral=True)
            return
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=self.shelf_view.original_embed, view=self.shelf_view)
        else:
            await interaction.response.edit_message(embed=self.shelf_view.original_embed, view=self.shelf_view)
        # Send ephemeral confirmation if provided
        if confirmation:
            await interaction.followup.send(confirmation, ephemeral=True)

    async def _update_progress(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ProgressUpdateModal(self))

    async def _finish(self, interaction: discord.Interaction):
        progress = get_user_book_progress(str(interaction.user.id), self.title, self.guild_id, self.author)
        if not progress or progress.get("status") != "reading":
            await interaction.response.send_message("That book is no longer marked as currently reading.", ephemeral=True)
            return
        await interaction.response.defer()
        ok = await run_book_action(action_finish_book,
            str(interaction.user.id), str(interaction.user.display_name), self.guild_id,
            {"title": self.title, "author": self.author},
        )
        if not ok:
            await interaction.followup.send("I couldn't mark that book finished. Try again.", ephemeral=True)
            return
        achievements = check_achievements(str(interaction.user.id), str(interaction.user.display_name), self.guild_id)
        confirmation = f"Marked **{self.title}** as finished. Ready to rate it?"
        await self._revert_to_shelf(interaction, confirmation)
        if list_next_book_choices(str(interaction.user.id), self.guild_id):
            await interaction.followup.send(
                "Want to start something from your lists?",
                view=OfferNextBookView(interaction.user.id, self.guild_id),
                ephemeral=True,
            )
        if achievements and interaction.channel:
            await announce_achievements(interaction.channel, str(interaction.user.display_name), achievements)

    async def _rate(self, interaction: discord.Interaction):
        progress = get_user_book_progress(str(interaction.user.id), self.title, self.guild_id, self.author)
        if not progress or progress.get("status") != "finished":
            await interaction.response.send_message("Finish the book before rating it.", ephemeral=True)
            return
        await interaction.response.send_modal(RatingModal(self))

    async def _dnf(self, interaction: discord.Interaction):
        progress = get_user_book_progress(str(interaction.user.id), self.title, self.guild_id, self.author)
        if not progress or progress.get("status") != "reading":
            await interaction.response.send_message("That book is no longer marked as currently reading.", ephemeral=True)
            return
        await interaction.response.defer()
        ok, _, _ = await run_book_action(action_dnf_book,
            str(interaction.user.id), str(interaction.user.display_name), self.guild_id,
            {"title": self.title, "author": self.author},
        )
        if not ok:
            await interaction.followup.send("I couldn't mark that book as DNF'd. Try again.", ephemeral=True)
            return
        achievements = check_achievements(str(interaction.user.id), str(interaction.user.display_name), self.guild_id)
        confirmation = f"Laid **{self.title}** to rest. No shame — time is precious."
        await self._revert_to_shelf(interaction, confirmation)
        if list_next_book_choices(str(interaction.user.id), self.guild_id):
            await interaction.followup.send(
                "Want to start something from your lists?",
                view=OfferNextBookView(interaction.user.id, self.guild_id),
                ephemeral=True,
            )
        if achievements and interaction.channel:
            await announce_achievements(interaction.channel, str(interaction.user.display_name), achievements)

    async def _offer_next(self, interaction: discord.Interaction):
        await send_next_book_picker(interaction, self.owner_id, self.guild_id)

    async def _done(self, interaction: discord.Interaction):
        await self._revert_to_shelf(interaction, "Back to your shelf.")
