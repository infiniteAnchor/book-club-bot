"""Discord client entrypoint: event handlers and startup wiring.

Slash commands live in commands.py (registered here via register), the
message-handler layer in handlers.py, UI classes in views.py, and all
scheduled jobs in jobs.py. This file only wires them together.
"""
import discord
import json
import asyncio
import logging
from weakref import WeakValueDictionary
from datetime import datetime

import errors
import llm
from llm import run as run_llm
import telemetry
import jobs
from commands import register
from rate_limit import MEMBER_RATE, UNENROLLED_RATE, TokenBucketLimiter
from config import (
    DISCORD_TOKEN,
    DEBUG_INTENT,
    CONVERSATIONAL_RESPONSES,
)
from database import (
    init_db, upsert_server,
)
from guild_settings import ensure_guild_settings, get_guild_settings
from helpers import (
    channel_matches_setting,
    channel_setting_label,
    get_reply_channel,
    resolve_text_channel,
    typing_indicator,
    upgrade_guild_channel_settings,
)
from actions import build_user_context, get_access_tier
from actions_nominations import (
    get_active_window,
    get_active_poll,
    get_active_coordination_poll,
)
from actions_social import (
    TRACKING_FEATURE,
    get_member_feature_preferences,
    get_single_participation_guild,
)
from embeds import send_embeds
from help_content import build_unknown_help_embed
from sending import (
    GUEST_DENIED_MESSAGE,
    INCOMPLETE_ACTION_MESSAGE,
    PARTIAL_ACTION_MESSAGE,
    make_enrollment_embed,
    send_notice,
)
from release_announcements import sync_release_boards
from views import (
    CoordinationView,
    EnrollmentView,
    NominateView,
    VotingView,
    _temporary_enrollment_messages,
)
from poll_workflows import (
    close_book_poll_workflow,
    close_coordination_poll_workflow,
    close_nominations_workflow,
)
from handlers import (
    GUEST_BLOCKED_ACTIONS,
    OPT_IN_EXEMPT_ACTIONS,
    RequestContext,
    _ACTION_HANDLERS,
    _send_response,
    plan_dispatch,
)
from jobs import (
    notify_admin,
    cancel_guild_jobs,
    ready_lock,
    recover_missed_year_in_review,
    schedule_auto_close,
    schedule_coordination_poll_auto_close,
    schedule_guild_jobs,
    schedule_llm_failure_alert,
    schedule_poll_auto_close,
    schedule_taste_profile_updates,
    schedule_year_in_review,
    schedule_reminder_sweep,
)
from participation_roles import (
    handle_member_update,
    restore_on_rejoin,
    run_startup_sync,
)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
errors.install_log_redaction()

client = discord.Client(intents=intents)
command_tree = discord.app_commands.CommandTree(client)
_commands_synced = False
_message_locks = WeakValueDictionary()
# guild_id -> name already upserted this process; skips per-message DB writes.
_registered_guilds: dict[str, str] = {}
_llm_limiter = TokenBucketLimiter()
# Longest slice of a flagged message forwarded to the admin alert DM.
INJECTION_ALERT_MAX_CHARS = 300
RATE_LIMITED_MESSAGE = (
    "You're sending messages faster than I can keep up. Give it a minute and try again."
)


def _register_guild(guild_id: str, name: str) -> None:
    """Upsert the server row and settings once per guild name per process."""
    if _registered_guilds.get(guild_id) == name:
        return
    upsert_server(guild_id, name)
    ensure_guild_settings(guild_id)
    _registered_guilds[guild_id] = name


def _is_enrolled(author_id: str, guild_id: str) -> bool:
    """Whether the member gets the full chat budget (sync, DB). True when opt-in is off."""
    if not get_guild_settings(guild_id)["member_opt_in_enabled"]:
        return True
    return bool(get_member_feature_preferences(author_id, guild_id)[TRACKING_FEATURE])


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _listen_filter_state(guild_id: str, channel_id: str) -> tuple[dict, bool]:
    """Guild settings and poll-thread membership for the channel filter (sync, DB)."""
    from poll_threads import get_poll_thread_by_discord_id
    return get_guild_settings(guild_id), bool(get_poll_thread_by_discord_id(guild_id, channel_id))

jobs.set_client(client)
register(command_tree)
llm.set_llm_error_handler(schedule_llm_failure_alert)


async def _on_app_command_error(interaction: discord.Interaction, error: Exception):
    """Central slash-command error path: log everything, answer with a safe fallback."""
    await errors.handle_interaction_failure(interaction, error)


command_tree.on_error = _on_app_command_error


async def ensure_enrollment_panel(guild):
    """Publish one persistent enrollment panel in the configured channel."""
    await jobs.sync_enrollment_panel(str(guild.id))


async def restore_open_items():
    """Re-register persistent views and auto-close jobs for any open polls."""
    for guild in client.guilds:
        guild_id = str(guild.id)
        fallback = resolve_text_channel(guild, get_guild_settings(guild_id)["nominations_channel"])

        window = get_active_window(guild_id)
        if window:
            closes_at_dt = datetime.fromisoformat(window["closes_at"])
            if closes_at_dt <= datetime.now(closes_at_dt.tzinfo):
                await close_nominations_workflow(guild, guild_id, fallback, manual=False)
            else:
                schedule_auto_close(guild_id, window["closes_at"], guild)

        poll = get_active_poll(guild_id)
        if poll and poll.get("nominations"):
            closes_at_dt = datetime.fromisoformat(poll["closes_at"])
            if closes_at_dt <= datetime.now(closes_at_dt.tzinfo):
                await close_book_poll_workflow(
                    guild, guild_id, poll.get("opened_by") or "auto-close", fallback, manual=False,
                )
            else:
                client.add_view(VotingView(guild_id, poll["poll_id"], poll["nominations"], timeout=None))
                schedule_poll_auto_close(guild_id, poll["closes_at"], guild)

        coord_poll = get_active_coordination_poll(guild_id)
        if coord_poll and coord_poll.get("options"):
            closes_at_dt = datetime.fromisoformat(coord_poll["closes_at"])
            if closes_at_dt <= datetime.now(closes_at_dt.tzinfo):
                await close_coordination_poll_workflow(guild, guild_id, fallback, manual=False)
            else:
                client.add_view(CoordinationView(guild_id, coord_poll["poll_id"], coord_poll["options"], timeout=None))
                schedule_coordination_poll_auto_close(guild_id, coord_poll["closes_at"], guild)


@client.event
async def on_ready():
    async with ready_lock():
        await _on_ready_startup()


async def _on_ready_startup():
    global _commands_synced
    init_db()
    if not _commands_synced:
        await command_tree.sync()
        _commands_synced = True
    client.add_view(EnrollmentView())
    client.add_view(NominateView())
    for guild in client.guilds:
        guild_id = str(guild.id)
        _register_guild(guild_id, guild.name)
        upgrade_guild_channel_settings(guild)
        await ensure_enrollment_panel(guild)
        schedule_guild_jobs(guild_id)
        try:
            await run_startup_sync(guild)
        except Exception as exc:
            errors.log_exception(exc, surface="role_sync", guild_id=guild_id)
    await sync_release_boards(client.guilds)
    await restore_open_items()
    jobs.restore_poll_thread_deletions()
    schedule_taste_profile_updates()
    schedule_year_in_review()
    schedule_reminder_sweep()
    await recover_missed_year_in_review()
    print(f"Logged in as {client.user}")


@client.event
async def on_guild_join(guild):
    guild_id = str(guild.id)
    _register_guild(guild_id, guild.name)
    upgrade_guild_channel_settings(guild)
    await ensure_enrollment_panel(guild)
    await sync_release_boards([guild])
    schedule_guild_jobs(guild_id)
    try:
        await run_startup_sync(guild)
    except Exception as exc:
        errors.log_exception(exc, surface="role_sync", guild_id=guild_id)


@client.event
async def on_guild_remove(guild):
    cancel_guild_jobs(str(guild.id))


@client.event
async def on_member_join(member):
    if member.bot or not member.guild:
        return
    try:
        await restore_on_rejoin(member)
    except Exception as exc:
        errors.log_exception(exc, surface="role_sync", guild_id=str(member.guild.id))
    settings = get_guild_settings(str(member.guild.id))
    channel = resolve_text_channel(member.guild, settings["opt_in_welcome_channel"])
    if channel is None:
        return
    msg = await channel.send(content=member.mention, embed=make_enrollment_embed(), view=EnrollmentView())
    _temporary_enrollment_messages.add(msg.id)


@client.event
async def on_member_update(before, after):
    if before.guild is None:
        return
    try:
        await handle_member_update(before, after)
    except Exception as exc:
        errors.log_exception(exc, surface="role_sync", guild_id=str(after.guild.id))


def _gate_action(action, author_id: str, guild_id: str, settings: dict, guild) -> str | None:
    """Return a denial key when this member may not run this action, else None."""
    # Preference management and admin-only handlers are intentionally exempt.
    # The handlers remain responsible for their own admin authorization.
    if settings["member_opt_in_enabled"] and action not in OPT_IN_EXEMPT_ACTIONS:
        preferences = get_member_feature_preferences(author_id, guild_id)
        if not preferences[TRACKING_FEATURE]:
            return "opt_in"
    # Guest tier restriction — write actions blocked
    if action in GUEST_BLOCKED_ACTIONS and get_access_tier(author_id) == "guest":
        return "guest"
    return None


def _opt_in_notice(settings: dict, guild):
    welcome = channel_setting_label(settings["opt_in_welcome_channel"], guild, empty="#welcome")
    if guild:
        reply = f"Join the book club with the enrollment panel in {welcome} before using bot features."
    else:
        reply = (
            "To use bot features in DMs, first join the book club using the enrollment "
            f"buttons in {welcome} in your server."
        )
    return reply, {"title": "👋 Join Book Club", "kind": "activity"}


_DENIAL_NOTICES = {
    "opt_in": _opt_in_notice,
    "guest": lambda _settings, _guild: (GUEST_DENIED_MESSAGE, {}),
}


@client.event
async def on_message(message):
    action = None
    guild_id = None
    metrics_token = None
    message_lock = None
    lock_acquired = False
    try:
        # Ignore every bot and webhook, including this one, so bots can't loop or spend LLM budget.
        if message.author.bot:
            return

        # DMs are the reply path for inactivity nudges, so channel filtering only
        # applies to messages sent inside a server.
        if message.guild:
            guild_settings_row, in_poll_thread = await asyncio.to_thread(
                _listen_filter_state, str(message.guild.id), str(message.channel.id)
            )
            if (
                guild_settings_row["listen_channel_only"]
                and not channel_matches_setting(
                    message.channel, guild_settings_row["listen_channel_name"]
                )
                and not in_poll_thread
            ):
                return

            # A user cannot naturally @mention the bot in a DM.  Keep the mention
            # requirement for server messages, but accept DM replies to nudges.
            if guild_settings_row["require_mention"] and client.user not in message.mentions:
                return

        guild_id = str(message.guild.id) if message.guild else get_single_participation_guild(str(message.author.id))
        if guild_id is None:
            await send_notice(
                get_reply_channel(message),
                "I need a server enrollment before I can use book-club features in DMs.",
            )
            return
        if message.guild:
            await asyncio.to_thread(_register_guild, guild_id, message.guild.name)

        # Budget check before any LLM work. Members who haven't joined keep a small
        # budget so they can still join, ask for help or check the current read.
        author_id = str(message.author.id)
        enrolled = await asyncio.to_thread(_is_enrolled, author_id, guild_id)
        capacity, period = MEMBER_RATE if enrolled else UNENROLLED_RATE
        bucket_key = (guild_id, author_id)
        if not _llm_limiter.allow(bucket_key, capacity, period):
            if _llm_limiter.should_notify(bucket_key):
                await send_notice(get_reply_channel(message), RATE_LIMITED_MESSAGE)
            return

        metrics_token = telemetry.start_request()
        # Keep successive messages from one reader ordered, including their context reads.
        key = (guild_id, str(message.author.id))
        message_lock = _message_locks.setdefault(key, asyncio.Lock())
        await message_lock.acquire()
        lock_acquired = True
        user_context = await asyncio.to_thread(build_user_context, str(message.author.id), guild_id)

        ctx = RequestContext(
            message=message,
            guild_id=guild_id,
            author_id=str(message.author.id),
            display_name=str(message.author.display_name),
            reply_channel=get_reply_channel(message),
        )

        asyncio.create_task(message.add_reaction("👀"))

        async with typing_indicator(ctx.reply_channel):
            intents = await run_llm(llm.get_intents, message.content, user_context)

        if DEBUG_INTENT:
            print(f"\n--- Intents from: {message.author} ---")
            print(json.dumps(intents, indent=2))
            print("---")

        if any(intent.get("flag_injection") for intent in intents):
            await notify_admin(
                client,
                f"Prompt injection flag from {message.author} ({message.author.id}) in guild {guild_id}: "
                f"{_truncate(message.content, INJECTION_ALERT_MAX_CHARS)}"
            )

        action = intents[0].get("action")
        planned = plan_dispatch(intents)
        if not planned:
            return

        if planned[0].get("action") == "unknown":
            intent = planned[0]
            action = "unknown"
            if intent.get("response") == llm.ENGLISH_ONLY_MESSAGE:
                await send_notice(ctx.reply_channel, intent["response"])
            elif CONVERSATIONAL_RESPONSES:
                await send_embeds(ctx.reply_channel, [build_unknown_help_embed(intent["response"])])
            else:
                await _send_response(ctx, intent)
            return

        settings = get_guild_settings(guild_id)
        denials_sent = set()
        # plan_dispatch drops fragments Claude couldn't parse when others were actionable.
        dropped_unknown = any(intent.get("action") == "unknown" for intent in intents)
        all_ran = not dropped_unknown
        # Actions run in order and an exception stops the rest on purpose: later
        # requests often depend on earlier ones ("finish Dune, rate it 9").
        for index, intent in enumerate(planned):
            action = intent.get("action")
            denial = _gate_action(action, str(message.author.id), guild_id, settings, message.guild)
            if denial:
                all_ran = False
                if denial not in denials_sent:
                    denials_sent.add(denial)
                    reply, kwargs = _DENIAL_NOTICES[denial](settings, message.guild)
                    await send_notice(ctx.reply_channel, reply, **kwargs)
                continue

            handler = _ACTION_HANDLERS.get(action) if isinstance(action, str) else None
            if not handler:
                all_ran = False
                await send_notice(ctx.reply_channel, INCOMPLETE_ACTION_MESSAGE)
                continue
            # Earlier actions may have changed the shelf, so rebuild context for later ones.
            if index > 0:
                user_context = await asyncio.to_thread(build_user_context, author_id, guild_id)
            # Handlers mutate user_context during re-intent passes; give each its own
            # copy, and tell a re-intent which of several requests it is answering.
            action_context = dict(user_context)
            if len(planned) > 1:
                action_context["focus_action"] = action
            await handler(ctx, intent.get("args", {}), intent, action_context)

        if dropped_unknown:
            await send_notice(ctx.reply_channel, PARTIAL_ACTION_MESSAGE)
        if all_ran:
            asyncio.create_task(message.remove_reaction("👀", client.user))
            asyncio.create_task(message.add_reaction("✅"))
    except asyncio.CancelledError:
        telemetry.mark_failure()
        raise
    except Exception as exc:
        telemetry.mark_failure()
        await errors.handle_message_failure(
            message,
            exc,
            action=action,
            guild_id=guild_id or (str(message.guild.id) if message.guild else None),
            user=f"{message.author} ({message.author.id})",
        )

    finally:
        if lock_acquired:
            message_lock.release()
        if metrics_token is not None:
            safe_action = action if isinstance(action, str) and action in llm.VALID_ACTIONS else None
            telemetry.finish_request(metrics_token, safe_action)


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is required")
    # log_handler=None: discord.py logs go through the root handler, which redacts secrets.
    client.run(DISCORD_TOKEN, log_handler=None)
