"""Poll voting Views (book poll + coordination) and nomination submit UI."""
import discord

import errors
from actions import get_access_tier, run_book_action
from actions_nominations import apply_nomination, cast_coordination_vote, cast_vote
from actions_social import has_club
from guild_settings import get_guild_settings
from sending import (
    GUEST_DENIED_MESSAGE,
    NOMINATION_NO_WINDOW,
    make_nomination_embed,
    nomination_denied_message,
)
from views_queue import SaveBookDestinationView


NOMINATION_TITLE_MAX_LENGTH = 200
NOMINATION_AUTHOR_MAX_LENGTH = 200
NOMINATION_REASON_MAX_LENGTH = 500
VOTE_BUTTON_LABEL_MAX_LENGTH = 80


def _vote_button_label(title: str) -> str:
    if len(title) <= VOTE_BUTTON_LABEL_MAX_LENGTH:
        return title
    return title[: VOTE_BUTTON_LABEL_MAX_LENGTH - 1].rstrip() + "…"


class VotingView(errors.SafeUIMixin, discord.ui.View):
    """A View with buttons for voting on book nominations."""

    def __init__(self, guild_id: str, poll_id: int, nominations: list, timeout=None):
        super().__init__(timeout=timeout)
        self.guild_id = guild_id
        self.poll_id = poll_id
        self.nominations = {nom["nomination_id"]: nom for nom in nominations}

        # Create a button for each nomination
        for nom in nominations:
            button = discord.ui.Button(
                label=_vote_button_label(str(nom["title"])),
                style=discord.ButtonStyle.primary,
                custom_id=f"vote_{poll_id}_{nom['nomination_id']}"
            )
            button.callback = self._make_vote_callback(nom["nomination_id"], nom["title"])
            self.add_item(button)

    def _make_vote_callback(self, nomination_id: int, title: str):
        async def callback(interaction: discord.Interaction):
            discord_id = str(interaction.user.id)
            display_name = str(interaction.user.display_name)
            if get_guild_settings(self.guild_id)["member_opt_in_enabled"] and not has_club(discord_id, self.guild_id):
                await interaction.response.send_message(
                    "Join the Book Club tier with the enrollment panel before voting.", ephemeral=True
                )
                return
            if get_access_tier(discord_id) == "guest":
                await interaction.response.send_message(
                    GUEST_DENIED_MESSAGE,
                    ephemeral=True
                )
                return
            success, msg = cast_vote(discord_id, display_name, self.guild_id, nomination_id, self.poll_id)

            if success and msg == "updated":
                await interaction.response.send_message(
                    f"Vote changed to **{title}**!",
                    ephemeral=True
                )
            elif success:
                await interaction.response.send_message(
                    f"Your vote for **{title}** has been recorded!",
                    ephemeral=True
                )
            elif msg == "no_poll":
                await interaction.response.send_message(
                    "There's no active poll right now.",
                    ephemeral=True
                )
            elif msg == "invalid_nomination":
                await interaction.response.send_message(
                    "That vote button is from an older poll and can no longer be used.",
                    ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "Something went wrong recording your vote.",
                    ephemeral=True
                )

        return callback


class CoordinationView(errors.SafeUIMixin, discord.ui.View):
    """A View with buttons for a coordination poll (pace/date/scheduling)."""

    def __init__(self, guild_id: str, poll_id: int, options: list, timeout=None):
        super().__init__(timeout=timeout)
        self.guild_id = guild_id
        self.poll_id = poll_id
        for opt in options:
            button = discord.ui.Button(
                label=opt["label"],
                style=discord.ButtonStyle.secondary,
                custom_id=f"coord_{poll_id}_{opt['option_id']}"
            )
            button.callback = self._make_callback(opt["option_id"], opt["label"])
            self.add_item(button)

    def _make_callback(self, option_id: int, label: str):
        async def callback(interaction: discord.Interaction):
            discord_id = str(interaction.user.id)
            display_name = str(interaction.user.display_name)
            if get_guild_settings(self.guild_id)["member_opt_in_enabled"] and not has_club(discord_id, self.guild_id):
                await interaction.response.send_message(
                    "Join the Book Club tier with the enrollment panel before voting.", ephemeral=True
                )
                return
            if get_access_tier(discord_id) == "guest":
                await interaction.response.send_message(
                    GUEST_DENIED_MESSAGE,
                    ephemeral=True
                )
                return
            success, msg = cast_coordination_vote(discord_id, display_name, self.guild_id, option_id, self.poll_id)
            if success and msg == "updated":
                await interaction.response.send_message(f"Vote changed to **{label}**!", ephemeral=True)
            elif success:
                await interaction.response.send_message(f"Your vote for **{label}** has been recorded!", ephemeral=True)
            elif msg == "no_poll":
                await interaction.response.send_message("There's no active poll right now.", ephemeral=True)
            elif msg == "invalid_option":
                await interaction.response.send_message("That vote button is from an older poll and can no longer be used.", ephemeral=True)
            else:
                await interaction.response.send_message("Something went wrong recording your vote.", ephemeral=True)
        return callback


async def submit_nomination_interaction(
    interaction: discord.Interaction, title: str | None, author: str | None, reason: str | None,
) -> None:
    """Shared slash/modal submit: Club/guest gates, then public nomination embed."""
    if not interaction.guild_id:
        await interaction.response.send_message(
            "Use this in your book-club server.", ephemeral=True,
        )
        return
    guild_id = str(interaction.guild_id)
    denied = nomination_denied_message(str(interaction.user.id), guild_id)
    if denied:
        await interaction.response.send_message(denied, ephemeral=True)
        return
    cleaned_title = (title or "").strip()
    cleaned_author = (author or "").strip() or None
    cleaned_reason = (reason or "").strip() or None
    if not cleaned_title:
        await interaction.response.send_message(
            "I need a book title to nominate.", ephemeral=True,
        )
        return
    await interaction.response.defer()
    status = await run_book_action(apply_nomination,
        str(interaction.user.id),
        str(interaction.user.display_name),
        guild_id,
        cleaned_title,
        cleaned_author,
        cleaned_reason,
        replace_if_present=True,
    )
    if status in ("ok", "replaced"):
        embed = make_nomination_embed(
            str(interaction.user.display_name),
            cleaned_title,
            cleaned_author,
            cleaned_reason,
            replaced=status == "replaced",
        )
        view = SaveBookDestinationView(
            interaction.user.id,
            guild_id,
            [{"title": cleaned_title, "author": cleaned_author}],
            source="nomination",
        )
        await interaction.followup.send(embed=embed, view=view)
        return
    if status == "no_window":
        await interaction.followup.send(NOMINATION_NO_WINDOW, ephemeral=True)
        return
    await interaction.followup.send(
        "Something went wrong submitting that nomination.", ephemeral=True,
    )


class NominateModal(errors.SafeUIMixin, discord.ui.Modal):
    """Collect a book nomination from the thread Nominate button."""

    def __init__(self):
        super().__init__(title="Nominate a book")
        self.book_title = discord.ui.TextInput(
            label="Title", placeholder="Piranesi", max_length=NOMINATION_TITLE_MAX_LENGTH,
        )
        self.author = discord.ui.TextInput(
            label="Author (optional)", required=False, placeholder="Susanna Clarke",
            max_length=NOMINATION_AUTHOR_MAX_LENGTH,
        )
        self.reason = discord.ui.TextInput(
            label="Why this book? (optional)",
            required=False,
            style=discord.TextStyle.paragraph,
            max_length=NOMINATION_REASON_MAX_LENGTH,
        )
        self.add_item(self.book_title)
        self.add_item(self.author)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        await submit_nomination_interaction(
            interaction,
            self.book_title.value,
            self.author.value,
            self.reason.value,
        )


class NominateView(errors.SafeUIMixin, discord.ui.View):
    """Persistent Nominate button for an open nomination thread."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Nominate", style=discord.ButtonStyle.primary, custom_id="nominate_open",
    )
    async def open_modal(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not interaction.guild_id:
            await interaction.response.send_message(
                "Use Nominate in your book-club server.", ephemeral=True,
            )
            return
        denied = nomination_denied_message(str(interaction.user.id), str(interaction.guild_id))
        if denied:
            await interaction.response.send_message(denied, ephemeral=True)
            return
        await interaction.response.send_modal(NominateModal())
