"""Views for private reading queue, wishlist save destinations, and next-book picks."""
from __future__ import annotations

import discord

import errors
from actions import get_access_tier, run_book_action
from actions_queue import (
    action_queue_list,
    action_queue_remove,
    action_save_book_to,
    action_start_from_choice,
    list_next_book_choices,
)
from actions_social import has_tracking
from embeds import make_embed
from guild_settings import get_guild_settings
from sending import GUEST_DENIED_MESSAGE


def _format_book_lines(items: list[dict], *, empty: str) -> str:
    if not items:
        return empty
    lines = []
    for item in items:
        author = item.get("author")
        suffix = f" — {author}" if author else ""
        lines.append(f"• **{item['title']}**{suffix}")
    return "\n".join(lines)


async def _owner_gate(interaction: discord.Interaction, owner_id: int, guild_id: str) -> bool:
    if interaction.user.id != owner_id:
        await interaction.response.send_message("Only you can use this.", ephemeral=True)
        return False
    if get_access_tier(str(interaction.user.id)) == "guest":
        await interaction.response.send_message(GUEST_DENIED_MESSAGE, ephemeral=True)
        return False
    if get_guild_settings(guild_id)["member_opt_in_enabled"]:
        if not has_tracking(str(interaction.user.id), guild_id):
            await interaction.response.send_message(
                "Join the book club before managing your reading lists.", ephemeral=True
            )
            return False
    return True


def private_queue_payload(owner_id: int | str, guild_id: str):
    """Embed plus optional manage view for the member's private queue."""
    items = action_queue_list(str(owner_id), guild_id)
    description = _format_book_lines(items, empty="Your private queue is empty.")
    embed = make_embed("🔒 Your private queue", description, kind="activity")
    view = PrivateQueueManageView(owner_id, guild_id, items) if items else None
    return embed, view


class RevealPrivateListView(errors.SafeUIMixin, discord.ui.View):
    """Channel ack control that reveals the private queue ephemerally."""

    def __init__(self, owner_id: int | str, guild_id: str):
        super().__init__(timeout=300)
        self.owner_id = int(owner_id)
        self.guild_id = guild_id

    @discord.ui.button(label="Show privately", style=discord.ButtonStyle.primary)
    async def show_privately(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not await _owner_gate(interaction, self.owner_id, self.guild_id):
            return
        embed, view = private_queue_payload(interaction.user.id, self.guild_id)
        kwargs = {"embed": embed, "ephemeral": True}
        if view is not None:
            kwargs["view"] = view
        await interaction.response.send_message(**kwargs)


class PrivateQueueManageView(errors.SafeUIMixin, discord.ui.View):
    """Ephemeral start/remove controls for the private queue."""

    def __init__(self, owner_id: int | str, guild_id: str, items: list[dict]):
        super().__init__(timeout=300)
        self.owner_id = int(owner_id)
        self.guild_id = guild_id
        self.items = items

        start_options = []
        remove_options = []
        for idx, item in enumerate(items[:25]):
            author = (item.get("author") or "")[:100]
            start_options.append(
                discord.SelectOption(
                    label=item["title"][:100],
                    description=author or "Start this book",
                    value=str(idx),
                )
            )
            remove_options.append(
                discord.SelectOption(
                    label=item["title"][:100],
                    description=author or "Remove from queue",
                    value=str(idx),
                )
            )
        self.start_select = discord.ui.Select(
            placeholder="Start a book…",
            options=start_options,
            custom_id="private_queue_start",
        )
        self.start_select.callback = self._on_start
        self.add_item(self.start_select)
        self.remove_select = discord.ui.Select(
            placeholder="Remove from queue…",
            options=remove_options,
            custom_id="private_queue_remove",
        )
        self.remove_select.callback = self._on_remove
        self.add_item(self.remove_select)

    async def _redraw_queue(self, interaction: discord.Interaction, confirmation: str) -> None:
        embed, view = private_queue_payload(self.owner_id, self.guild_id)
        body = embed.description or ""
        embed.description = f"{confirmation}\n\n{body}" if body else confirmation
        await interaction.edit_original_response(embed=embed, view=view)

    async def _on_start(self, interaction: discord.Interaction):
        if not await _owner_gate(interaction, self.owner_id, self.guild_id):
            return
        if not self.start_select.values:
            return
        item = self.items[int(self.start_select.values[0])].copy()
        await interaction.response.defer()
        ok = await run_book_action(action_start_from_choice,
            str(interaction.user.id),
            str(interaction.user.display_name),
            self.guild_id,
            list_name="queue",
            title=item["title"],
            author=item.get("author"),
        )
        if not ok:
            await interaction.followup.send(
                "I couldn't start that book. Try again.", ephemeral=True
            )
            return
        await self._redraw_queue(interaction, f"Started {item['title']}.")

    async def _on_remove(self, interaction: discord.Interaction):
        if not await _owner_gate(interaction, self.owner_id, self.guild_id):
            return
        if not self.remove_select.values:
            return
        item = self.items[int(self.remove_select.values[0])].copy()
        await interaction.response.defer()
        ok, status = await run_book_action(action_queue_remove,
            str(interaction.user.id),
            str(interaction.user.display_name),
            self.guild_id,
            {"title": item["title"], "author": item.get("author")},
        )
        if not ok or status != "removed":
            await interaction.followup.send(
                "I couldn't remove that book. Try again.", ephemeral=True
            )
            return
        await self._redraw_queue(interaction, f"Removed {item['title']}.")


async def send_next_book_picker(
    interaction: discord.Interaction, owner_id: int, guild_id: str
) -> None:
    """Open the combined ephemeral next-book picker for an interaction."""
    if not await _owner_gate(interaction, owner_id, guild_id):
        return
    choices = list_next_book_choices(str(interaction.user.id), guild_id)
    if not choices:
        await interaction.response.send_message(
            "Your private queue and wishlist are both empty.", ephemeral=True
        )
        return
    view = NextBookPickerView(owner_id, guild_id, choices)
    embed = make_embed(
        "📖 What next?",
        "Pick a book from your private queue or public wishlist.",
        kind="activity",
    )
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class OfferNextBookView(errors.SafeUIMixin, discord.ui.View):
    """Public finish/DNF control that opens the combined ephemeral picker."""

    def __init__(self, owner_id: int | str, guild_id: str):
        super().__init__(timeout=900)
        self.owner_id = int(owner_id)
        self.guild_id = guild_id

    @discord.ui.button(label="Pick next from your lists", style=discord.ButtonStyle.success)
    async def pick_next(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await send_next_book_picker(interaction, self.owner_id, self.guild_id)


class NextBookPickerView(errors.SafeUIMixin, discord.ui.View):
    """Ephemeral select to start a book from queue or wishlist."""

    def __init__(self, owner_id: int | str, guild_id: str, choices: list[dict]):
        super().__init__(timeout=300)
        self.owner_id = int(owner_id)
        self.guild_id = guild_id
        self.choices = choices
        options = []
        for idx, choice in enumerate(choices[:25]):
            label = choice["label"][:100]
            author = choice.get("author") or ""
            options.append(
                discord.SelectOption(
                    label=label,
                    description=(author[:100] if author else choice["list"][:100]),
                    value=str(idx),
                )
            )
        select = discord.ui.Select(placeholder="Start which book?", options=options)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        if not await _owner_gate(interaction, self.owner_id, self.guild_id):
            return
        values = interaction.data.get("values", []) if interaction.data else []
        if not values:
            return
        choice = self.choices[int(values[0])].copy()
        await interaction.response.defer()
        ok = await run_book_action(action_start_from_choice,
            str(interaction.user.id),
            str(interaction.user.display_name),
            self.guild_id,
            list_name=choice["list"],
            title=choice["title"],
            author=choice.get("author"),
        )
        if not ok:
            await interaction.followup.send(
                "I couldn't start that book. Try again.", ephemeral=True
            )
            return
        await interaction.edit_original_response(
            embed=make_embed(
                "📖 Started",
                f"Started **{choice['title']}** from your {choice['list']}.",
                kind="success",
            ),
            view=None,
        )


class SaveBookDestinationView(errors.SafeUIMixin, discord.ui.View):
    """Select a known book, then save to Queue or Wishlist."""

    def __init__(
        self,
        owner_id: int | str,
        guild_id: str,
        books: list[dict],
        *,
        source: str = "manual",
    ):
        super().__init__(timeout=600)
        self.owner_id = int(owner_id)
        self.guild_id = guild_id
        self.books = books
        self.source = source
        self.selected_idx: int | None = None if len(books) != 1 else 0

        if len(books) > 1:
            options = []
            for idx, book in enumerate(books[:25]):
                author = book.get("author") or ""
                options.append(
                    discord.SelectOption(
                        label=book["title"][:100],
                        description=author[:100],
                        value=str(idx),
                    )
                )
            select = discord.ui.Select(placeholder="Choose a book to save…", options=options)
            select.callback = self._on_select
            self.add_item(select)

        queue_btn = discord.ui.Button(label="Save to queue", style=discord.ButtonStyle.primary)
        queue_btn.callback = self._save_queue
        self.add_item(queue_btn)

        wish_btn = discord.ui.Button(label="Save to wishlist", style=discord.ButtonStyle.secondary)
        wish_btn.callback = self._save_wishlist
        self.add_item(wish_btn)

    async def _on_select(self, interaction: discord.Interaction):
        if not await _owner_gate(interaction, self.owner_id, self.guild_id):
            return
        values = interaction.data.get("values", []) if interaction.data else []
        if not values:
            return
        self.selected_idx = int(values[0])
        book = self.books[self.selected_idx]
        await interaction.response.send_message(
            f"Selected **{book['title']}**. Choose Queue or Wishlist.",
            ephemeral=True,
        )

    async def _save(self, interaction: discord.Interaction, destination: str):
        if not await _owner_gate(interaction, self.owner_id, self.guild_id):
            return
        if self.selected_idx is None:
            await interaction.response.send_message(
                "Pick a book from the menu first.", ephemeral=True
            )
            return
        book = self.books[self.selected_idx].copy()
        source = self.source
        await interaction.response.defer(ephemeral=True)
        ok, status = await run_book_action(action_save_book_to,
            destination,
            str(interaction.user.id),
            str(interaction.user.display_name),
            self.guild_id,
            {"title": book["title"], "author": book.get("author")},
            source=source,
        )
        if status == "full":
            await interaction.followup.send(
                f"Your {destination} is full (max 25). Remove something first.",
                ephemeral=True,
            )
            return
        if not ok:
            await interaction.followup.send(
                "I couldn't save that book. Try again.", ephemeral=True
            )
            return
        if status == "already":
            msg = f"**{book['title']}** is already on your {destination}."
        else:
            msg = f"Saved **{book['title']}** to your {destination}."
        await interaction.followup.send(msg, ephemeral=True)

    async def _save_queue(self, interaction: discord.Interaction):
        await self._save(interaction, "queue")

    async def _save_wishlist(self, interaction: discord.Interaction):
        await self._save(interaction, "wishlist")
