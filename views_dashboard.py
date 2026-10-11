"""Admin dashboard embed, settings modals, and AdminDashboardView."""
from typing import Any, cast

import discord

import errors
import jobs
from actions_nominations import (
    get_active_coordination_poll,
    get_active_poll,
    get_coordination_poll_results,
    get_nominations,
    get_poll_results,
)
from actions_social import (
    DM_NUDGES_FEATURE,
    TIER_PARTICIPATION_ARGS,
    clear_user_tone_override,
    describe_participation,
    get_admin_summary,
    get_guild_release_announcement_preference,
    get_member_feature_preferences,
    list_user_tone_overrides,
    set_guild_release_announcements_enabled,
    set_user_tone_override,
)
from config import RELEASE_ANNOUNCEMENTS_ENABLED
from embeds import make_embed
from guild_settings import (
    CHANNEL_SETTING_KEYS,
    SCHEDULE_KEYS,
    SettingsError,
    get_guild_settings,
    update_guild_settings,
)
from helpers import (
    _dashboard_date,
    channel_setting_edit_default,
    channel_setting_label,
    has_admin_role,
    resolve_channel_setting_to_id,
)
from participation_roles import (
    ADMIN_ROLE_KEY,
    MANAGED_ROLE_KEYS,
    ROLE_NAMES,
    ParticipationRoleError,
    adopt_existing_role,
    get_managed_role_ids,
    get_sync_completed_at,
    reconcile_guild,
    set_admin_role,
    set_participation,
)
from actions_session import get_active_session
from poll_workflows import (
    close_book_poll_workflow,
    close_coordination_poll_workflow,
    close_nominations_workflow,
    open_book_poll_workflow,
    open_coordination_poll_workflow,
    open_nominations_workflow,
)


FEATURE_TOGGLES = (
    ("member_opt_in_enabled", "Member opt-in"),
    ("combined_weekly_post", "Combined weekly post"),
    ("weekly_recap_scheduled", "Weekly recap"),
    ("checkin_prompt_scheduled", "Check-in prompts"),
    ("inactivity_nudge_scheduled", "Inactivity nudges"),
    ("taste_profile_scheduled", "Taste profiles"),
    ("require_mention", "Require @mention"),
    ("listen_channel_only", "Listen-channel only"),
)


def _on_off(value: bool) -> str:
    return "on" if value else "off"


def _channel_label(ref: str, guild=None, *, empty: str = "(not set)") -> str:
    return channel_setting_label(ref, guild, empty=empty)


def build_admin_dashboard_embed(guild_id: str, page: str = "overview", *, selected_member=None, guild=None):
    """Build one private admin dashboard page from fresh, Python-owned data."""
    summary = get_admin_summary(guild_id)
    if page == "overview":
        club_read = get_active_session(guild_id)
        if club_read:
            club_line = f"Club book: **{club_read['title']}**"
            if club_read.get("target_date"):
                club_line += f" (target {club_read['target_date']})"
        else:
            club_line = "Club book: none"
        lines = [
            f"Members tracked: **{summary['total_users']}**",
            f"Books finished (all time): **{summary['total_finished']}**",
            club_line,
            f"Personal shelves in progress: **{len(summary['currently_reading'])}**",
            "Use the buttons below to inspect live club activity, edit server settings, or run an admin workflow.",
        ]
        if summary["active_nomination_window"]:
            lines.append(f"Nominations close {_dashboard_date(summary['active_nomination_window']['closes_at'])}.")
        if summary["active_poll"]:
            label = "Tiebreaker" if summary["active_poll"]["is_tiebreaker"] else "Book poll"
            lines.append(f"{label} closes {_dashboard_date(summary['active_poll']['closes_at'])}.")
        if summary["active_coordination_poll"]:
            lines.append("A coordination poll is open.")
        return make_embed("🛠️ Club dashboard", "\n".join(lines), kind="info")

    meta = {
        "overview": ("🛠️ Club dashboard", "info"),
        "reading": ("📖 Current reads", "activity"),
        "nominations": ("🗳️ Nominations", "poll"),
        "book_poll": ("🗳️ Book poll", "poll"),
        "coordination": ("🗓️ Coordination poll", "poll"),
        "ratings": ("⭐ Recent ratings", "milestone"),
        "achievements": ("🏆 Recent achievements", "milestone"),
        "tones": ("🎭 Tone overrides", "info"),
        "actions": ("⚙️ Admin actions", "info"),
        "settings": ("⚙️ Server settings", "info"),
        "roles": ("👥 Roles", "info"),
        "adopt": ("🔗 Adopt existing role", "info"),
        "confirm": ("⚠️ Confirm admin action", "info"),
    }
    title, kind = meta.get(page, meta["overview"])
    embed = make_embed(title, "", kind=kind)

    if page == "reading":
        club_read = get_active_session(guild_id)
        if club_read:
            target = f" · target {club_read['target_date']}" if club_read.get("target_date") else ""
            embed.description = f"Club book: **{club_read['title']}**{target}"
        for row in summary["currently_reading"]:
            progress = "No progress logged" if row["progress"] is None else f"{row['progress']} {row['unit'] or ''}".strip()
            embed.add_field(name=row["user"], value=f"**{row['title']}** — {progress}", inline=False)
        if not summary["currently_reading"]:
            embed.description = (embed.description + "\n" if embed.description else "") + "Nobody is currently marked as reading."
    elif page == "nominations":
        window = summary["active_nomination_window"]
        nominations = get_nominations(guild_id)
        embed.description = f"Closes {_dashboard_date(window['closes_at'])}." if window else "No nomination window is open."
        for nomination in nominations[:25]:
            author = f" by {nomination['author']}" if nomination.get("author") else ""
            detail = f"Nominated by {nomination['display_name']}"
            if nomination.get("reason"):
                detail += f" — {nomination['reason']}"
            embed.add_field(name=f"{nomination['title']}{author}", value=detail, inline=False)
    elif page == "book_poll":
        poll = get_active_poll(guild_id)
        if not poll:
            embed.description = "No book poll is open."
        else:
            label = "Tiebreaker poll" if poll["is_tiebreaker"] else "Book poll"
            embed.description = f"{label}; closes {_dashboard_date(poll['closes_at'])}."
            for item in get_poll_results(guild_id)[:25]:
                nomination = item["nomination"]
                author = f" by {nomination['author']}" if nomination.get("author") else ""
                embed.add_field(name=f"{nomination['title']}{author}", value=f"{item['vote_count']} vote(s)", inline=False)
    elif page == "coordination":
        poll = get_active_coordination_poll(guild_id)
        if not poll:
            embed.description = "No coordination poll is open."
        else:
            embed.description = f"**{poll['question']}**\nCloses {_dashboard_date(poll['closes_at'])}."
            for item in get_coordination_poll_results(guild_id)[:25]:
                embed.add_field(name=item["label"], value=f"{item['vote_count']} vote(s)", inline=False)
    elif page == "ratings":
        embed.description = "Ratings logged during the last seven days."
        for rating in summary["recent_ratings"][:25]:
            embed.add_field(name=rating["user"], value=f"**{rating['title']}** — {rating['score']}/10", inline=False)
        if not summary["recent_ratings"]:
            embed.description = "No ratings have been logged in the last seven days."
    elif page == "achievements":
        embed.description = "Achievements earned during the last seven days."
        for achievement in summary["recent_achievements"][:25]:
            embed.add_field(name=achievement["user"], value=achievement["achievement"], inline=False)
        if not summary["recent_achievements"]:
            embed.description = "No achievements have been earned in the last seven days."
    elif page == "tones":
        tones = list_user_tone_overrides(guild_id)
        for tone in tones[:25]:
            embed.add_field(name=tone["display_name"], value=f"{tone['mode']} (set by {tone['set_by']})", inline=False)
        if not tones:
            embed.description = "No active tone overrides."
    elif page == "actions":
        embed.description = "Open workflows collect their required input in a modal. Closing or changing a member tone requires confirmation."
    elif page == "settings":
        settings = get_guild_settings(guild_id)
        embed.description = (
            "These values apply only to this server. New servers copy the current "
            "`config.py` defaults; later default changes do not rewrite this row."
        )
        embed.add_field(
            name="Features",
            value=(
                f"Member opt-in: **{_on_off(settings['member_opt_in_enabled'])}**\n"
                f"Combined weekly post: **{_on_off(settings['combined_weekly_post'])}**\n"
                f"Weekly recap: **{_on_off(settings['weekly_recap_scheduled'])}**\n"
                f"Check-in prompts: **{_on_off(settings['checkin_prompt_scheduled'])}**\n"
                f"Inactivity nudges: **{_on_off(settings['inactivity_nudge_scheduled'])}**\n"
                f"Taste profiles: **{_on_off(settings['taste_profile_scheduled'])}**"
            ),
            inline=False,
        )
        embed.add_field(
            name="Channels",
            value=(
                f"Nominations: {_channel_label(settings['nominations_channel'], guild)}\n"
                f"Weekly recap: {_channel_label(settings['weekly_recap_channel'], guild)}\n"
                f"Welcome: {_channel_label(settings['opt_in_welcome_channel'], guild)}\n"
                f"Reply: {_channel_label(settings['reply_channel_name'], guild, empty='(same channel)')}\n"
                f"Listen: {_channel_label(settings['listen_channel_name'], guild, empty='(all channels)')}\n"
                f"Check-in: {_channel_label(settings['checkin_prompt_channel'], guild)}\n"
                f"Year in review: {_channel_label(settings['year_in_review_channel'], guild)}\n"
                f"Release notices: {_channel_label(settings['release_announcements_channel'], guild)}"
            ),
            inline=False,
        )
        embed.add_field(
            name="Schedule and durations",
            value=(
                f"Weekly recap: **{settings['weekly_recap_day_of_week']} {settings['weekly_recap_hour']}:00**\n"
                f"Check-in: **{settings['checkin_prompt_day_of_week']} {settings['checkin_prompt_hour']}:00**\n"
                f"Nudge after **{settings['inactivity_nudge_days']}** days, every **{settings['inactivity_nudge_interval_hours']}** hours\n"
                f"Nomination / poll / coordination defaults: **{settings['nominations_default_days']} / {settings['poll_default_days']} / {settings['coordination_poll_default_days']}** days\n"
                f"Poll thread retention: **{settings['poll_thread_retention_days']}** days (0 = keep forever)\n"
                f"Require @mention: **{_on_off(settings['require_mention'])}** · Listen-channel only: **{_on_off(settings['listen_channel_only'])}**"
            ),
            inline=False,
        )
        if settings["combined_weekly_post"]:
            embed.add_field(
                name="Note",
                value="Combined weekly post is on, so the separate recap and check-in jobs stay idle.",
                inline=False,
            )
    elif page == "roles":
        ids = get_managed_role_ids(guild_id)
        synced = get_sync_completed_at(guild_id)
        lines = [f"Last full sync: **{_dashboard_date(synced) if synced else 'never'}**"]
        for key in MANAGED_ROLE_KEYS:
            stored = ids.get(key)
            lines.append(f"{ROLE_NAMES[key]}: `{stored or 'not registered'}`")
        embed.description = "\n".join(lines)
        if selected_member:
            prefs = get_member_feature_preferences(str(selected_member.id), guild_id)
            admin_ids = get_managed_role_ids(guild_id)
            admin_id = admin_ids.get(ADMIN_ROLE_KEY)
            has_admin = bool(
                admin_id and any(str(role.id) == str(admin_id) for role in getattr(selected_member, "roles", []))
            )
            embed.add_field(
                name=selected_member.display_name,
                value=f"{describe_participation(prefs)}\nBook Club Admin: **{'yes' if has_admin else 'no'}**",
                inline=False,
            )
        else:
            embed.add_field(
                name="Selected member",
                value="Pick a member to change their tier, nudges, or admin role.",
                inline=False,
            )
    elif page == "adopt":
        embed.description = (
            "Point a managed Book Club role at an existing Discord role. "
            "The bot will not delete, rename, or auto-adopt roles by name."
        )
    return embed


def _text_input(label, value, *, placeholder="", required=False, max_length=100):
    kwargs = {"label": label, "required": required, "max_length": max_length}
    if placeholder:
        kwargs["placeholder"] = placeholder
    if value is not None and str(value) != "":
        kwargs["default"] = str(value)
    return discord.ui.TextInput(**kwargs)


class DashboardChannelsModal(errors.SafeUIMixin, discord.ui.Modal):
    def __init__(self, dashboard):
        super().__init__(title="Edit main channels")
        self.dashboard = dashboard
        settings = get_guild_settings(dashboard.guild_id)
        guild = getattr(dashboard, "guild", None)
        self.nominations = _text_input(
            "Nominations channel",
            channel_setting_edit_default(guild, settings["nominations_channel"]),
            required=True,
        )
        self.recap = _text_input(
            "Weekly recap channel",
            channel_setting_edit_default(guild, settings["weekly_recap_channel"]),
            required=True,
        )
        self.welcome = _text_input(
            "Welcome / enrollment channel",
            channel_setting_edit_default(guild, settings["opt_in_welcome_channel"]),
            required=True,
        )
        self.reply = _text_input(
            "Reply channel (blank = same)",
            channel_setting_edit_default(guild, settings["reply_channel_name"]),
        )
        self.listen = _text_input(
            "Listen channel (blank = all)",
            channel_setting_edit_default(guild, settings["listen_channel_name"]),
        )
        for item in (self.nominations, self.recap, self.welcome, self.reply, self.listen):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        await self.dashboard._apply_settings_from_modal(
            interaction,
            nominations_channel=self.nominations.value,
            weekly_recap_channel=self.recap.value,
            opt_in_welcome_channel=self.welcome.value,
            reply_channel_name=self.reply.value,
            listen_channel_name=self.listen.value,
        )


class DashboardExtraChannelsModal(errors.SafeUIMixin, discord.ui.Modal):
    def __init__(self, dashboard):
        super().__init__(title="Edit more channels")
        self.dashboard = dashboard
        settings = get_guild_settings(dashboard.guild_id)
        guild = getattr(dashboard, "guild", None)
        self.checkin = _text_input(
            "Check-in channel",
            channel_setting_edit_default(guild, settings["checkin_prompt_channel"]),
            required=True,
        )
        self.year = _text_input(
            "Year-in-review channel",
            channel_setting_edit_default(guild, settings["year_in_review_channel"]),
            required=True,
        )
        self.leaderboard = _text_input(
            "Leaderboard channel",
            channel_setting_edit_default(guild, settings["leaderboard_channel"]),
            required=True,
        )
        self.release = _text_input(
            "Release notices channel",
            channel_setting_edit_default(guild, settings["release_announcements_channel"]),
            required=True,
        )
        for item in (self.checkin, self.year, self.leaderboard, self.release):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        await self.dashboard._apply_settings_from_modal(
            interaction,
            checkin_prompt_channel=self.checkin.value,
            year_in_review_channel=self.year.value,
            leaderboard_channel=self.leaderboard.value,
            release_announcements_channel=self.release.value,
        )


class DashboardScheduleModal(errors.SafeUIMixin, discord.ui.Modal):
    def __init__(self, dashboard):
        super().__init__(title="Edit schedule")
        self.dashboard = dashboard
        settings = get_guild_settings(dashboard.guild_id)
        self.recap_day = _text_input("Weekly recap day (mon-sun)", settings["weekly_recap_day_of_week"], required=True, max_length=3)
        self.recap_hour = _text_input("Weekly recap hour (0-23)", settings["weekly_recap_hour"], required=True, max_length=2)
        self.checkin_day = _text_input("Check-in day (mon-sun)", settings["checkin_prompt_day_of_week"], required=True, max_length=3)
        self.checkin_hour = _text_input("Check-in hour (0-23)", settings["checkin_prompt_hour"], required=True, max_length=2)
        self.nudge_hours = _text_input("Nudge interval hours", settings["inactivity_nudge_interval_hours"], required=True, max_length=5)
        for item in (self.recap_day, self.recap_hour, self.checkin_day, self.checkin_hour, self.nudge_hours):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        await self.dashboard._apply_settings_from_modal(
            interaction,
            weekly_recap_day_of_week=self.recap_day.value,
            weekly_recap_hour=self.recap_hour.value,
            checkin_prompt_day_of_week=self.checkin_day.value,
            checkin_prompt_hour=self.checkin_hour.value,
            inactivity_nudge_interval_hours=self.nudge_hours.value,
        )


class DashboardDurationsModal(errors.SafeUIMixin, discord.ui.Modal):
    def __init__(self, dashboard):
        super().__init__(title="Edit durations")
        self.dashboard = dashboard
        settings = get_guild_settings(dashboard.guild_id)
        self.noms = _text_input("Nomination window days", settings["nominations_default_days"], required=True, max_length=3)
        self.poll = _text_input("Book poll days", settings["poll_default_days"], required=True, max_length=3)
        self.coord = _text_input("Coordination poll days", settings["coordination_poll_default_days"], required=True, max_length=3)
        self.nudge_days = _text_input("Nudge after days idle", settings["inactivity_nudge_days"], required=True, max_length=3)
        self.archive = _text_input("Thread archive minutes", settings["thread_auto_archive_minutes"], required=True, max_length=5)
        self.retention = _text_input("Poll retention days (0=forever)", settings["poll_thread_retention_days"], required=True, max_length=3)
        for item in (self.noms, self.poll, self.coord, self.nudge_days, self.archive, self.retention):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        await self.dashboard._apply_settings_from_modal(
            interaction,
            nominations_default_days=self.noms.value,
            poll_default_days=self.poll.value,
            coordination_poll_default_days=self.coord.value,
            inactivity_nudge_days=self.nudge_days.value,
            thread_auto_archive_minutes=self.archive.value,
            poll_thread_retention_days=self.retention.value,
        )


class DashboardDaysModal(errors.SafeUIMixin, discord.ui.Modal):
    def __init__(self, dashboard, action: str):
        super().__init__(title="Open nominations" if action == "open_nominations" else "Open book poll")
        self.dashboard = dashboard
        self.action = action
        self.days = discord.ui.TextInput(label="Duration in days", required=False, placeholder="Leave blank for the configured default", max_length=3)
        self.add_item(self.days)

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.days.value.strip()
        try:
            days = int(raw) if raw else None
            if days is not None and days < 1:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("Duration must be a positive whole number of days.", ephemeral=True)
            return
        await self.dashboard.perform_open(interaction, self.action, days=days)


class DashboardCoordinationModal(errors.SafeUIMixin, discord.ui.Modal):
    def __init__(self, dashboard):
        super().__init__(title="Open coordination poll")
        self.dashboard = dashboard
        self.question = discord.ui.TextInput(label="Question", max_length=200)
        self.options = discord.ui.TextInput(label="Options (one per line)", style=discord.TextStyle.paragraph, max_length=1000)
        self.days = discord.ui.TextInput(label="Duration in days", required=False, placeholder="Leave blank for the configured default", max_length=3)
        self.add_item(self.question)
        self.add_item(self.options)
        self.add_item(self.days)

    async def on_submit(self, interaction: discord.Interaction):
        options = [item.strip() for item in self.options.value.splitlines() if item.strip()]
        raw = self.days.value.strip()
        try:
            days = int(raw) if raw else None
            if days is not None and days < 1:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("Duration must be a positive whole number of days.", ephemeral=True)
            return
        if not self.question.value.strip() or len(options) < 2 or len(options) > 25:
            await interaction.response.send_message("Provide a question and between 2 and 25 non-empty options.", ephemeral=True)
            return
        await self.dashboard.perform_open(interaction, "open_coordination", days=days, question=self.question.value.strip(), options=options)


class AdminDashboardView(errors.SafeUIMixin, discord.ui.View):
    """Private, invoker-owned control surface for the existing admin workflows."""
    def __init__(self, guild_id: str, owner_id: int, guild=None):
        super().__init__(timeout=900)
        self.guild_id = guild_id
        self.owner_id = owner_id
        self.guild = guild
        self.page = "overview"
        self.message: Any = None
        self.pending = None
        self.return_page = "actions"
        self.selected_member: discord.Member | None = None
        self.selected_role_key: str | None = None
        self.selected_discord_role = None
        self.render()

    def _dashboard_embed(self, page: str | None = None, *, selected_member=None):
        member = self.selected_member if selected_member is None else selected_member
        return build_admin_dashboard_embed(
            self.guild_id,
            page or self.page,
            selected_member=member,
            guild=self.guild,
        )

    def _resolve_channel_updates(self, changes: dict) -> dict:
        """Turn typed channel names into snowflake IDs when the guild is available."""
        if self.guild is None:
            return changes
        resolved = dict(changes)
        for key, value in changes.items():
            if key not in CHANNEL_SETTING_KEYS:
                continue
            text = (value or "").strip()
            if not text:
                resolved[key] = ""
                continue
            channel_id = resolve_channel_setting_to_id(self.guild, text)
            if channel_id is None:
                name = text[1:].strip() if text.startswith("#") else text
                raise SettingsError(f"No text channel named {name}.")
            resolved[key] = channel_id
        return resolved

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Only the admin who opened this dashboard can use its controls.", ephemeral=True)
            return False
        if not interaction.guild or not has_admin_role(interaction.user):
            await interaction.response.send_message("Only server admins can use this dashboard.", ephemeral=True)
            return False
        self.guild = interaction.guild
        return True

    def _add_button(self, label, callback, *, style=discord.ButtonStyle.secondary, row=None):
        button = discord.ui.Button(label=label, style=style, row=row)
        button.callback = callback
        self.add_item(button)

    def render(self):
        self.clear_items()
        if self.page == "overview":
            for label, page in (("Current Reads", "reading"), ("Nominations", "nominations"), ("Book Poll", "book_poll"), ("Coordination", "coordination"), ("Recent Ratings", "ratings"), ("Achievements", "achievements"), ("Tone Overrides", "tones"), ("Server settings", "settings"), ("Roles", "roles")):
                self._add_button(label, self._show(page))
            self._add_button("Actions", self._show("actions"), style=discord.ButtonStyle.primary)
            self._add_button("Refresh", self._refresh_dashboard, style=discord.ButtonStyle.primary)
        elif self.page == "settings":
            self._add_feature_toggle_select()
            self._add_settings_edit_select()
            self._add_button("Back", self._show("overview"), style=discord.ButtonStyle.primary)
            self._add_button("Refresh", self._refresh_dashboard, style=discord.ButtonStyle.primary)
        elif self.page == "actions":
            self._add_button("Open nominations", self._open_nominations, style=discord.ButtonStyle.success)
            self._add_button("Close nominations", self._confirm("close_nominations"), style=discord.ButtonStyle.danger)
            self._add_button("Open book poll", self._open_poll, style=discord.ButtonStyle.success)
            self._add_button("Close book poll", self._confirm("close_poll"), style=discord.ButtonStyle.danger)
            self._add_button("Open coordination", self._open_coordination, style=discord.ButtonStyle.success)
            self._add_button("Close coordination", self._confirm("close_coordination"), style=discord.ButtonStyle.danger)
            preference = get_guild_release_announcement_preference(self.guild_id)
            notices_enabled = preference is not False
            label = "Disable release notices" if notices_enabled else "Enable release notices"
            self._add_button(label, self._toggle_release_notices, row=2)
            self._add_tone_select(row=3)
            if self.selected_member:
                self._add_button("Glaze selected", self._confirm("tone_glaze"), style=discord.ButtonStyle.success, row=4)
                self._add_button("Roast selected", self._confirm("tone_roast"), style=discord.ButtonStyle.secondary, row=4)
                self._add_button("Neutral selected", self._confirm("tone_neutral"), row=4)
                self._add_button("Clear selected", self._confirm("tone_clear"), style=discord.ButtonStyle.danger, row=4)
            self._add_button("Back", self._show("overview"), style=discord.ButtonStyle.primary, row=4)
        elif self.page == "roles":
            self._add_member_select()
            if self.selected_member:
                self._add_button("Tracking", self._set_member_tier("tracking"), row=1)
                self._add_button("Updates", self._set_member_tier("updates"), row=1)
                self._add_button("Club", self._set_member_tier("club"), row=1)
                self._add_button("Chat only", self._set_member_tier("chat"), style=discord.ButtonStyle.secondary, row=1)
                self._add_button("Toggle nudges", self._toggle_member_nudges, style=discord.ButtonStyle.success, row=1)
                self._add_button("Grant admin", self._confirm("admin_grant", return_page="roles"), style=discord.ButtonStyle.success, row=2)
                self._add_button("Remove admin", self._confirm("admin_revoke", return_page="roles"), style=discord.ButtonStyle.danger, row=2)
            self._add_button("Sync roles", self._confirm("sync_roles", return_page="roles"), row=3)
            self._add_button("Adopt existing role", self._show("adopt"), row=3)
            self._add_button("Back", self._show("overview"), style=discord.ButtonStyle.primary, row=4)
            self._add_button("Refresh", self._refresh_dashboard, style=discord.ButtonStyle.primary, row=4)
        elif self.page == "adopt":
            self._add_adopt_key_select()
            self._add_adopt_role_select()
            self._add_button("Adopt", self._confirm_adopt, style=discord.ButtonStyle.danger, row=2)
            self._add_button("Back", self._show("roles"), style=discord.ButtonStyle.primary, row=2)
        elif self.page == "confirm":
            self._add_button("Confirm", self._execute_pending, style=discord.ButtonStyle.danger)
            self._add_button("Cancel", self._cancel_pending)
        else:
            self._add_button("Back", self._show("overview"), style=discord.ButtonStyle.primary)
            self._add_button("Refresh", self._refresh_dashboard, style=discord.ButtonStyle.primary)

    def _add_tone_select(self, *, row=2):
        select = discord.ui.UserSelect(placeholder="Select a member to manage their tone", min_values=1, max_values=1, row=row)
        async def select_callback(interaction):
            self.selected_member = discord.utils.get(interaction.guild.members, id=select.values[0].id) if interaction.guild else None
            self.render()
            await interaction.response.edit_message(embed=self._dashboard_embed("actions"), view=self)
        select.callback = select_callback
        self.add_item(select)

    def _add_member_select(self):
        select = discord.ui.UserSelect(placeholder="Select a member to manage", min_values=1, max_values=1, row=0)

        async def select_callback(interaction):
            self.selected_member = discord.utils.get(interaction.guild.members, id=select.values[0].id) if interaction.guild else None
            self.render()
            await interaction.response.edit_message(
                embed=self._dashboard_embed("roles"),
                view=self,
            )

        select.callback = select_callback
        self.add_item(select)

    def _add_adopt_key_select(self):
        options = [
            discord.SelectOption(
                label=ROLE_NAMES[key],
                value=key,
                default=key == self.selected_role_key,
            )
            for key in MANAGED_ROLE_KEYS
        ]
        select = discord.ui.Select(placeholder="Managed role to replace", min_values=1, max_values=1, options=options, row=0)

        async def callback(interaction):
            self.selected_role_key = select.values[0]
            await interaction.response.defer()

        select.callback = callback
        self.add_item(select)

    def _add_adopt_role_select(self):
        select = discord.ui.RoleSelect(placeholder="Existing Discord role to adopt", min_values=1, max_values=1, row=1)

        async def callback(interaction):
            self.selected_discord_role = select.values[0]
            await interaction.response.defer()

        select.callback = callback
        self.add_item(select)

    def _set_member_tier(self, tier: str):
        async def callback(interaction):
            if not await self._require_selected_member(interaction):
                return
            await self._apply_member_participation(interaction, **TIER_PARTICIPATION_ARGS[tier])

        return callback

    async def _toggle_member_nudges(self, interaction):
        if not await self._require_selected_member(interaction):
            return
        member = self.selected_member
        assert member is not None
        prefs = get_member_feature_preferences(str(member.id), self.guild_id)
        await self._apply_member_participation(interaction, dm_nudges=not prefs[DM_NUDGES_FEATURE])

    async def _require_selected_member(self, interaction) -> bool:
        if self.selected_member is None:
            await interaction.response.send_message("Select a member first.", ephemeral=True)
            return False
        return True

    async def _apply_member_participation(self, interaction, **kwargs):
        member = self.selected_member
        if member is None:
            await interaction.response.send_message("Select a member first.", ephemeral=True)
            return
        try:
            prefs = await set_participation(member, **kwargs)
        except ParticipationRoleError:
            await interaction.response.send_message(
                "I couldn't update Discord roles. Check Manage Roles and role order.",
                ephemeral=True,
            )
            return
        self.render()
        await interaction.response.edit_message(
            embed=self._dashboard_embed("roles"),
            view=self,
        )
        await interaction.followup.send(describe_participation(prefs), ephemeral=True)

    async def _confirm_adopt(self, interaction):
        if not self.selected_role_key or self.selected_discord_role is None:
            await interaction.response.send_message(
                "Choose both the managed role and an existing Discord role first.",
                ephemeral=True,
            )
            return
        await self._confirm("adopt_role", return_page="adopt")(interaction)

    def _add_feature_toggle_select(self):
        options = [
            discord.SelectOption(label=label, value=key)
            for key, label in FEATURE_TOGGLES
        ]
        select = discord.ui.Select(placeholder="Toggle a feature", min_values=1, max_values=1, options=options, row=0)

        async def callback(interaction):
            key = select.values[0]
            current = get_guild_settings(self.guild_id)[key]
            await self._save_settings(interaction, **{key: not current})

        select.callback = callback
        self.add_item(select)

    def _add_settings_edit_select(self):
        select = discord.ui.Select(
            placeholder="Edit channels, schedule, or durations",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="Main channels", value="channels"),
                discord.SelectOption(label="More channels", value="extra_channels"),
                discord.SelectOption(label="Schedule", value="schedule"),
                discord.SelectOption(label="Durations", value="durations"),
            ],
            row=1,
        )

        async def callback(interaction):
            kind = select.values[0]
            if kind == "channels":
                await interaction.response.send_modal(DashboardChannelsModal(self))
            elif kind == "extra_channels":
                await interaction.response.send_modal(DashboardExtraChannelsModal(self))
            elif kind == "schedule":
                await interaction.response.send_modal(DashboardScheduleModal(self))
            else:
                await interaction.response.send_modal(DashboardDurationsModal(self))

        select.callback = callback
        self.add_item(select)

    async def _save_settings(self, interaction, **changes):
        try:
            changes = self._resolve_channel_updates(changes)
            update_guild_settings(self.guild_id, **changes)
        except SettingsError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.defer()
        if SCHEDULE_KEYS.intersection(changes):
            jobs.schedule_guild_jobs(self.guild_id)
        if {"member_opt_in_enabled", "opt_in_welcome_channel"}.intersection(changes):
            await jobs.sync_enrollment_panel(self.guild_id)
        self.render()
        await interaction.edit_original_response(
            embed=self._dashboard_embed("settings"), view=self
        )

    async def _apply_settings_from_modal(self, interaction, **changes):
        try:
            changes = self._resolve_channel_updates(changes)
            update_guild_settings(self.guild_id, **changes)
        except SettingsError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        if SCHEDULE_KEYS.intersection(changes):
            jobs.schedule_guild_jobs(self.guild_id)
        if {"member_opt_in_enabled", "opt_in_welcome_channel"}.intersection(changes):
            await jobs.sync_enrollment_panel(self.guild_id)
        self.render()
        if self.message:
            await cast(Any, self.message).edit(
                embed=self._dashboard_embed("settings"), view=self
            )
        await interaction.followup.send("Server settings updated.", ephemeral=True)

    def _show(self, page):
        async def callback(interaction):
            self.page, self.pending = page, None
            self.render()
            await interaction.response.edit_message(
                embed=self._dashboard_embed(page),
                view=self,
            )
        return callback

    async def _refresh_dashboard(self, interaction):
        self.render()
        await interaction.response.edit_message(
            embed=self._dashboard_embed(),
            view=self,
        )

    async def _open_nominations(self, interaction):
        await interaction.response.send_modal(DashboardDaysModal(self, "open_nominations"))

    async def _open_poll(self, interaction):
        await interaction.response.send_modal(DashboardDaysModal(self, "open_poll"))

    async def _open_coordination(self, interaction):
        await interaction.response.send_modal(DashboardCoordinationModal(self))

    async def _toggle_release_notices(self, interaction):
        preference = get_guild_release_announcement_preference(self.guild_id)
        enabled = preference is False
        set_guild_release_announcements_enabled(self.guild_id, enabled)
        if not RELEASE_ANNOUNCEMENTS_ENABLED:
            status = " The global `RELEASE_ANNOUNCEMENTS_ENABLED` setting is currently off."
        else:
            status = ""
        await interaction.response.send_message(
            f"Release announcements are {'enabled' if enabled else 'disabled'} for this server.{status}",
            ephemeral=True,
        )
        self.render()
        if self.message:
            await cast(Any, self.message).edit(
                embed=self._dashboard_embed("actions"), view=self
            )

    def _confirm(self, action, *, return_page="actions"):
        async def callback(interaction):
            if action.startswith("tone_") and not self.selected_member:
                await interaction.response.send_message("Select a member first.", ephemeral=True)
                return
            if action in ("admin_grant", "admin_revoke") and not self.selected_member:
                await interaction.response.send_message("Select a member first.", ephemeral=True)
                return
            self.pending = action
            self.return_page = return_page
            self.page = "confirm"
            self.render()
            embed = make_embed("⚠️ Confirm admin action", self._confirm_copy(action), kind="info")
            await interaction.response.edit_message(embed=embed, view=self)
        return callback

    def _confirm_copy(self, action: str) -> str:
        member_name = self.selected_member.display_name if self.selected_member else "this member"
        if action == "sync_roles":
            return "Reconcile Discord participation roles with the database for every member?"
        if action == "adopt_role" and self.selected_role_key and self.selected_discord_role:
            return (
                f"Use **{self.selected_discord_role.name}** as **{ROLE_NAMES[self.selected_role_key]}**? "
                "Neither role will be deleted or renamed."
            )
        if action == "admin_grant":
            return f"Grant **{ROLE_NAMES[ADMIN_ROLE_KEY]}** to **{member_name}**?"
        if action == "admin_revoke":
            return f"Remove **{ROLE_NAMES[ADMIN_ROLE_KEY]}** from **{member_name}**?"
        target = f" for {member_name}" if action.startswith("tone_") and self.selected_member else ""
        return f"Confirm **{action.replace('_', ' ')}**{target}?"

    async def _cancel_pending(self, interaction):
        self.pending = None
        self.page = self.return_page
        self.render()
        await interaction.response.edit_message(
            embed=self._dashboard_embed(),
            view=self,
        )

    async def _execute_pending(self, interaction):
        action = self.pending
        await interaction.response.defer()
        await self.perform_action(interaction, action)

    async def _edit_overview(self):
        await self._edit_page("overview")

    async def _edit_page(self, page: str):
        self.page, self.pending = page, None
        self.render()
        if self.message:
            await cast(Any, self.message).edit(
                embed=self._dashboard_embed(page),
                view=self,
            )

    async def perform_open(self, interaction, action, *, days: int | None = None, question: str | None = None, options: list[str] | None = None):
        if interaction.user.id != self.owner_id or not interaction.guild or not has_admin_role(interaction.user):
            await interaction.response.send_message("Only the dashboard owner may run this admin action.", ephemeral=True)
            return
        await interaction.response.defer()
        guild = interaction.guild
        opened_by = str(interaction.user.id)
        fallback = interaction.channel
        if action == "open_nominations":
            result = await open_nominations_workflow(guild, self.guild_id, opened_by, days, fallback)
            if not result.ok:
                await interaction.followup.send("A nomination window is already open.", ephemeral=True)
                return
        elif action == "open_poll":
            result = await open_book_poll_workflow(guild, self.guild_id, opened_by, days, fallback)
            if not result.ok:
                await interaction.followup.send(
                    "A book poll is already open, or there are no closed nominations to vote on.",
                    ephemeral=True,
                )
                return
        else:
            result = await open_coordination_poll_workflow(
                guild, self.guild_id, opened_by, question, options or [], days, fallback,
            )
            if not result.ok:
                if result.reason == "invalid_options":
                    await interaction.followup.send("Provide a question and at least two options.", ephemeral=True)
                    return
                await interaction.followup.send("A coordination poll is already open.", ephemeral=True)
                return
        await self._edit_overview()

    async def perform_action(self, interaction, action):
        guild = interaction.guild
        fallback = interaction.channel
        if action == "sync_roles":
            try:
                summary = await reconcile_guild(guild)
            except ParticipationRoleError as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
                await self._edit_page("roles")
                return
            await interaction.followup.send(
                f"Roles synced. Restored Chat on {summary['restored_chat']} member(s); "
                f"repaired {summary['repaired']} hierarchy issue(s).",
                ephemeral=True,
            )
            await self._edit_page("roles")
            return
        if action == "adopt_role":
            role_key = self.selected_role_key
            adopted = self.selected_discord_role
            if not role_key or adopted is None:
                await interaction.followup.send(
                    "Choose both the managed role and an existing Discord role first.",
                    ephemeral=True,
                )
                await self._edit_page("adopt")
                return
            try:
                await adopt_existing_role(guild, role_key, adopted)
                summary = await reconcile_guild(guild)
            except ParticipationRoleError as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
                await self._edit_page("adopt")
                return
            await interaction.followup.send(
                f"Adopted **{adopted.name}** as **{ROLE_NAMES[role_key]}**. "
                f"Then synced roles (Chat restored on {summary['restored_chat']}, repaired {summary['repaired']}).",
                ephemeral=True,
            )
            await self._edit_page("roles")
            return
        if action in ("admin_grant", "admin_revoke"):
            member = self.selected_member
            if not member:
                await interaction.followup.send("Select a member before changing admin status.", ephemeral=True)
                await self._edit_page("roles")
                return
            result = await set_admin_role(member, action == "admin_grant")
            messages = {
                "granted": f"Granted **{ROLE_NAMES[ADMIN_ROLE_KEY]}** to **{member.display_name}**.",
                "revoked": f"Removed **{ROLE_NAMES[ADMIN_ROLE_KEY]}** from **{member.display_name}**.",
                "unchanged": f"**{member.display_name}** already has that admin status.",
                "last_admin": "The last Book Club Admin cannot be removed.",
                "missing_role": "The Book Club Admin role is not registered yet. Use Sync roles first.",
            }
            await interaction.followup.send(messages[result], ephemeral=True)
            await self._edit_page("roles")
            return
        if action == "close_nominations":
            result = await close_nominations_workflow(guild, self.guild_id, fallback)
            if not result.ok:
                await interaction.followup.send("There is no nomination window open to close.", ephemeral=True)
                return
        elif action == "close_poll":
            result = await close_book_poll_workflow(guild, self.guild_id, str(interaction.user.id), fallback)
            if not result.ok:
                await interaction.followup.send("There is no book poll open to close.", ephemeral=True)
                return
        elif action == "close_coordination":
            result = await close_coordination_poll_workflow(guild, self.guild_id, fallback)
            if not result.ok:
                await interaction.followup.send("There is no coordination poll open to close.", ephemeral=True)
                return
        else:
            member = self.selected_member
            if not member:
                await interaction.followup.send("Select a member before changing a tone override.", ephemeral=True)
                return
            if action == "tone_clear":
                clear_user_tone_override(str(member.id), self.guild_id)
            else:
                mode = action.removeprefix("tone_")
                set_user_tone_override(str(member.id), member.display_name, self.guild_id, mode, str(interaction.user.display_name))
        await self._edit_overview()

    async def on_timeout(self):
        for child in self.children:
            cast(Any, child).disabled = True
        if self.message:
            try:
                await cast(Any, self.message).edit(view=self)
            except discord.HTTPException:
                pass

