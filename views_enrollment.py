"""Enrollment panel View and join-message tracking."""
import discord

import errors
from actions import get_access_tier
from actions_social import (
    DM_NUDGES_FEATURE,
    TIER_PARTICIPATION_ARGS,
    TRACKING_FEATURE,
    describe_participation,
    get_member_feature_preferences,
)
from participation_roles import ParticipationRoleError, set_participation

# Track enrollment messages from on_member_join that should be deleted after interaction
_temporary_enrollment_messages: set = set()


async def _interaction_member(interaction: discord.Interaction):
    """Return the invoking guild member, or None outside a server."""
    if not interaction.guild:
        return None
    user = interaction.user
    if isinstance(user, discord.Member):
        return user
    return interaction.guild.get_member(user.id)


class EnrollmentView(errors.SafeUIMixin, discord.ui.View):
    """Stateless persistent controls for each member's own enrollment."""

    def __init__(self):
        super().__init__(timeout=None)

    async def _apply(self, interaction: discord.Interaction, **kwargs):
        member = await _interaction_member(interaction)
        if member is None:
            await interaction.response.send_message(
                "Please use the enrollment controls in your book-club server.", ephemeral=True
            )
            return
        try:
            preferences = await set_participation(member, **kwargs)
        except ParticipationRoleError:
            await interaction.response.send_message(
                "I couldn't update Discord roles. Ask an admin to check the bot's Manage Roles permission.",
                ephemeral=True,
            )
            return
        response = describe_participation(preferences)
        if get_access_tier(str(member.id)) == "guest" and preferences[TRACKING_FEATURE]:
            response += " Your guest access can still restrict writing and voting."
        await interaction.response.send_message(response, ephemeral=True)
        if interaction.message and interaction.message.id in _temporary_enrollment_messages:
            _temporary_enrollment_messages.discard(interaction.message.id)
            try:
                await interaction.message.delete()
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Tracking", style=discord.ButtonStyle.primary, custom_id="enroll_tracking", row=0)
    async def enroll_tracking(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._apply(interaction, **TIER_PARTICIPATION_ARGS["tracking"])

    @discord.ui.button(label="Updates", style=discord.ButtonStyle.primary, custom_id="enroll_updates", row=0)
    async def enroll_updates(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._apply(interaction, **TIER_PARTICIPATION_ARGS["updates"])

    @discord.ui.button(label="Club", style=discord.ButtonStyle.primary, custom_id="enroll_club", row=0)
    async def enroll_club(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._apply(interaction, **TIER_PARTICIPATION_ARGS["club"])

    @discord.ui.button(label="Chat only", style=discord.ButtonStyle.secondary, custom_id="enroll_chat", row=1)
    async def enroll_chat(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._apply(interaction, **TIER_PARTICIPATION_ARGS["chat"])

    @discord.ui.button(label="Toggle nudges", style=discord.ButtonStyle.success, custom_id="enroll_nudges", row=1)
    async def enroll_nudges(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not interaction.guild_id:
            await interaction.response.send_message(
                "Please use the enrollment controls in your book-club server.", ephemeral=True
            )
            return
        prefs = get_member_feature_preferences(str(interaction.user.id), str(interaction.guild_id))
        await self._apply(interaction, dm_nudges=not prefs[DM_NUDGES_FEATURE])

