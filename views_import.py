"""Goodreads CSV import preview + confirm button, and shelf export embed."""
from typing import Any, cast

import discord

import errors
from actions import get_access_tier
from actions_import import (
    ExportResult,
    ImportApplyResult,
    ImportPreview,
    ImportRow,
    apply_goodreads_import,
)
from actions_social import has_tracking
from embeds import make_embed
from guild_settings import get_guild_settings


IMPORT_GUEST_MESSAGE = (
    "Guests can't import a Goodreads library. Join with the enrollment panel first."
)
IMPORT_TRACKING_MESSAGE = (
    "Join the book club with the enrollment panel before importing your Goodreads history."
)
EXPORT_GUEST_MESSAGE = (
    "Guests can't export a library. Join with the enrollment panel first."
)
EXPORT_TRACKING_MESSAGE = (
    "Join the book club with the enrollment panel before exporting your shelf."
)


IMPORT_PRIVACY_COPY = (
    "This fills in recommendations and your personal shelf. "
    "`/history`, weekly recaps, and the leaderboard stay about books logged in Discord — "
    "they will not become your Goodreads."
)


def build_import_preview_embed(preview: ImportPreview) -> discord.Embed:
    lines = [
        IMPORT_PRIVACY_COPY,
        "",
        f"**{len(preview.to_add)}** finished to add",
        f"**{preview.already_on_shelf}** already on the shelf",
        f"**{preview.to_read_ignored}** to-read ignored",
        f"**{preview.currently_reading_ignored}** currently-reading ignored",
    ]
    if preview.dnf_ignored:
        lines.append(f"**{preview.dnf_ignored}** DNF (bookshelves `dnf`) ignored")
    if preview.other_ignored:
        lines.append(f"**{preview.other_ignored}** other shelves ignored")
    if preview.blank_skipped:
        lines.append(f"**{preview.blank_skipped}** blank titles skipped")
    if preview.truncated:
        lines.append("Only the first 2,000 finished rows from this file can be imported. Run `/import` again with a smaller export if you need the rest.")
    if not preview.to_add:
        lines.append("")
        lines.append("Nothing new to import.")
    return make_embed("📥 Goodreads import", "\n".join(lines), kind="info")


EXPORT_PRIVACY_COPY = (
    "This is your personal shelf in this server. Private queue titles are included "
    "as to-read. Uploading the file to another site will share those titles there."
)


def build_export_embed(result: ExportResult) -> discord.Embed:
    if result.row_count == 0:
        return make_embed(
            "📤 Shelf export",
            "Nothing on your shelf to export yet. Start a book, finish one, or "
            "`/import` a Goodreads history first.",
            kind="info",
        )
    lines = [
        EXPORT_PRIVACY_COPY,
        "",
        f"**{result.row_count}** book{'s' if result.row_count != 1 else ''} in this file",
        f"**{result.finished}** finished · **{result.reading}** currently reading · "
        f"**{result.to_read}** to-read",
    ]
    if result.dnf:
        lines.append(f"**{result.dnf}** DNF (marked read, bookshelves `dnf`)")
    if result.queue_included:
        lines.append(f"**{result.queue_included}** from your private queue")
    if result.truncated:
        lines.append("Only the first 2,000 rows are included.")
    lines.append("")
    lines.append("Goodreads, StoryGraph, and Hardcover can import this CSV.")
    return make_embed("📤 Shelf export", "\n".join(lines), kind="success")


def build_import_result_embed(result: ImportApplyResult) -> discord.Embed:
    lines = [
        IMPORT_PRIVACY_COPY,
        "",
        f"Imported **{result.added}** finished book{'s' if result.added != 1 else ''}.",
    ]
    if result.ratings_added:
        lines.append(f"Saved **{result.ratings_added}** rating{'s' if result.ratings_added != 1 else ''}.")
    if result.already_on_shelf:
        lines.append(f"**{result.already_on_shelf}** already on the shelf.")
    return make_embed("📥 Goodreads import", "\n".join(lines), kind="success")


class GoodreadsImportView(errors.SafeUIMixin, discord.ui.View):
    """Holds parsed rows in memory until confirm or timeout (restart requires `/import` again)."""

    def __init__(
        self,
        *,
        guild_id: str,
        owner_id: int,
        display_name: str,
        rows: list[ImportRow],
    ):
        super().__init__(timeout=900)
        self.guild_id = guild_id
        self.owner_id = owner_id
        self.display_name = display_name
        self.rows = rows
        self.message: Any = None
        self._applied = False
        self._in_progress = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the person who ran `/import` can confirm this upload.",
                ephemeral=True,
            )
            return False
        if get_access_tier(str(interaction.user.id)) == "guest":
            await interaction.response.send_message(IMPORT_GUEST_MESSAGE, ephemeral=True)
            return False
        if get_guild_settings(self.guild_id)["member_opt_in_enabled"]:
            if not has_tracking(str(interaction.user.id), self.guild_id):
                await interaction.response.send_message(
                    IMPORT_TRACKING_MESSAGE, ephemeral=True
                )
                return False
        return True

    def _disable_controls(self) -> None:
        for child in self.children:
            cast(Any, child).disabled = True
        self.stop()

    @discord.ui.button(label="Import", style=discord.ButtonStyle.success)
    async def confirm_import(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if self._applied or self._in_progress:
            await interaction.response.send_message("That import already ran. Use `/import` again if you need to.", ephemeral=True)
            return
        self._in_progress = True
        try:
            await interaction.response.defer()
            result = apply_goodreads_import(
                str(interaction.user.id),
                self.display_name,
                self.guild_id,
                self.rows,
            )
            self._applied = True
            self.rows = []
            self._disable_controls()
            await interaction.edit_original_response(embed=build_import_result_embed(result), view=self)
        except Exception:
            if not self._applied:
                self._in_progress = False
            raise

    async def on_timeout(self) -> None:
        for child in self.children:
            cast(Any, child).disabled = True
        if self.message is None:
            return
        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            pass
