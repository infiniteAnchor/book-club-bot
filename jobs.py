"""Scheduling layer: APScheduler setup, job registries, and all run_/schedule_ jobs.

Owns the scheduler and the discord client reference (set once by bot.py via
set_client) so no other module needs to import bot.py. Keeps the per-guild
auto-close bookkeeping for nominations, polls, and coordination polls.
"""
import asyncio
import atexit
from datetime import datetime, timedelta, timezone

import discord
from apscheduler.schedulers.background import BackgroundScheduler

import errors

from config import (
    ADMIN_DISCORD_ID,
    LLM_ALERT_COOLDOWN_MINUTES,
)
import llm
from llm import run as run_llm
from actions import get_user_reading_history, log_event
from actions_nominations import (
    get_active_coordination_poll,
    get_active_poll,
    get_active_window,
)
from actions_social import (
    deliver_inactivity_nudge,
    get_all_active_users,
    get_inactivity_nudge_groups,
    get_weekly_activity,
    get_year_activity,
    update_user_taste_profile,
)
from database import connect
from guild_settings import get_guild_settings
from helpers import resolve_text_channel
from sending import (
    ENROLLMENT_PANEL_TITLE,
    make_enrollment_embed,
    make_nudge_embed,
    make_reminder_embed,
    send_summary,
)
from actions_nudges import list_due_reminders, mark_reminder_delivered
from poll_threads import (
    delete_poll_thread,
    list_pending_deletions,
)

scheduler = BackgroundScheduler()
scheduler.start()
atexit.register(scheduler.shutdown)

# Populated once by bot.py after the discord client is created.
client: discord.Client = None  # type: ignore


def _watch_future(future, label: str) -> None:
    """Log exceptions from a coroutine scheduled on the bot loop.

    asyncio.run_coroutine_threadsafe returns a Future whose exceptions never
    surface in the calling (scheduler) thread unless retrieved — this callback
    retrieves and logs them so failures are visible but isolated.
    """
    def _done(fut):
        try:
            exc = fut.exception()
        except asyncio.CancelledError:
            return
        except Exception:
            return
        if exc is not None:
            errors.log_exception(exc, surface="scheduled_job", job=label)
    future.add_done_callback(_done)


def _launch(coro, label: str) -> None:
    """Run a coroutine on the bot loop from scheduler threads, isolating failure."""
    try:
        future = asyncio.run_coroutine_threadsafe(coro, client.loop)
    except Exception as exc:
        coro.close()
        errors.log_exception(exc, surface="scheduled_job", job=label)
        return
    _watch_future(future, label)


def set_client(discord_client: discord.Client) -> None:
    global client
    client = discord_client


# Job registries: guild_id -> APScheduler job id for auto-close jobs.
nomination_jobs = {}
poll_jobs = {}
coordination_poll_jobs = {}
poll_thread_deletion_jobs = {}

INACTIVITY_NUDGE_JOB_ID = "inactivity_nudge_sweep"
CHECKIN_PROMPT_JOB_ID = "periodic_checkin_prompt"
WEEKLY_RECAP_JOB_ID = "weekly_recap"
WEEKLY_POST_JOB_ID = "combined_weekly_post"
TASTE_PROFILE_JOB_ID = "quarterly_taste_profile_update"
YEAR_IN_REVIEW_JOB_ID = "year_in_review"
YEAR_IN_REVIEW_RECOVERY_GRACE = timedelta(days=1)
REMINDER_SWEEP_JOB_ID = "reminder_sweep"


def _guild_job_id(prefix: str, guild_id: str) -> str:
    return f"{prefix}:{guild_id}"


def _remove_job(job_id: str) -> None:
    if scheduler.get_job(job_id):
        try:
            scheduler.remove_job(job_id)
        except Exception:
            pass


def _add_cron_job(job_fn, job_id: str, day_of_week: str, hour: int) -> None:
    scheduler.add_job(
        job_fn,
        "cron",
        day_of_week=day_of_week,
        hour=hour,
        id=job_id,
        replace_existing=True,
        misfire_grace_time=3600,
    )


def _resolve_guild(guild_id: str):
    getter = getattr(client, "get_guild", None)
    if callable(getter):
        try:
            guild = getter(int(guild_id))
        except (TypeError, ValueError):
            guild = None
        if guild is not None:
            return guild
    return next((item for item in client.guilds if str(item.id) == str(guild_id)), None)


def _auto_close_job_id(kind: str, guild_id: str) -> str:
    return f"auto-close:{kind}:{guild_id}"


def _schedule_date_auto_close(kind: str, guild_id: str, closes_at, registry, run):
    """One date job per kind+guild. Overlapping schedule calls replace the same id."""
    if isinstance(closes_at, str):
        closes_at_dt = datetime.fromisoformat(closes_at)
    else:
        closes_at_dt = closes_at
    job_id = _auto_close_job_id(kind, guild_id)
    existing = registry.pop(guild_id, None)
    if existing and existing != job_id:
        _remove_job(existing)

    def job_fn():
        try:
            run()
        except Exception as exc:
            errors.log_exception(exc, surface="scheduled_job", job="auto_close", guild_id=guild_id)
        finally:
            registry.pop(guild_id, None)

    job = scheduler.add_job(
        job_fn,
        "date",
        run_date=closes_at_dt,
        id=job_id,
        replace_existing=True,
        misfire_grace_time=86400,
    )
    registry[guild_id] = job.id
    return job.id


def _cancel_auto_close(guild_id: str, registry, kind: str):
    existing = registry.pop(guild_id, None)
    if existing:
        _remove_job(existing)
    stable = _auto_close_job_id(kind, guild_id)
    if existing != stable:
        _remove_job(stable)


def _nominations_channel(guild, guild_id: str):
    return resolve_text_channel(guild, get_guild_settings(guild_id)["nominations_channel"])


def schedule_auto_close(guild_id: str, closes_at, guild):
    """Auto-close the nomination window through the shared workflow."""
    def run():
        from poll_workflows import close_nominations_workflow
        if not get_active_window(guild_id):
            return
        _launch(
            close_nominations_workflow(guild, guild_id, _nominations_channel(guild, guild_id), manual=False),
            "auto_close",
        )
    return _schedule_date_auto_close("nominations", guild_id, closes_at, nomination_jobs, run)


def cancel_auto_close(guild_id: str):
    _cancel_auto_close(guild_id, nomination_jobs, "nominations")


def schedule_poll_auto_close(guild_id: str, closes_at, guild):
    """Auto-close the book poll through the shared workflow (includes tiebreaker)."""
    def run():
        from poll_workflows import close_book_poll_workflow
        poll = get_active_poll(guild_id)
        if not poll:
            return
        opened_by = poll.get("opened_by") or "auto-close"
        _launch(
            close_book_poll_workflow(
                guild, guild_id, opened_by, _nominations_channel(guild, guild_id), manual=False,
            ),
            "auto_close",
        )
    return _schedule_date_auto_close("poll", guild_id, closes_at, poll_jobs, run)


def cancel_poll_auto_close(guild_id: str):
    _cancel_auto_close(guild_id, poll_jobs, "poll")


def schedule_coordination_poll_auto_close(guild_id: str, closes_at, guild):
    """Auto-close the coordination poll through the shared workflow."""
    def run():
        from poll_workflows import close_coordination_poll_workflow
        if not get_active_coordination_poll(guild_id):
            return
        _launch(
            close_coordination_poll_workflow(
                guild, guild_id, _nominations_channel(guild, guild_id), manual=False,
            ),
            "auto_close",
        )
    return _schedule_date_auto_close("coordination", guild_id, closes_at, coordination_poll_jobs, run)


def cancel_coordination_poll_auto_close(guild_id: str):
    _cancel_auto_close(guild_id, coordination_poll_jobs, "coordination")


def _thread_delete_job_id(record):
    return f"poll-thread-delete:{record['guild_id']}:{record['workflow_type']}:{record['workflow_id']}"


def schedule_poll_thread_deletion(record, guild, delete_after):
    job_id = _thread_delete_job_id(record)
    existing = poll_thread_deletion_jobs.pop(job_id, None)
    if existing:
        _remove_job(existing)

    def job_fn():
        _launch(_delete_poll_thread_job(guild, record, job_id), "poll_thread_delete")

    job = scheduler.add_job(job_fn, "date", run_date=delete_after, id=job_id,
                            replace_existing=True, misfire_grace_time=86400)
    poll_thread_deletion_jobs[job_id] = job.id
    return job.id


async def _delete_poll_thread_job(guild, record, job_id):
    try:
        await delete_poll_thread(guild, record)
    finally:
        poll_thread_deletion_jobs.pop(job_id, None)


def restore_poll_thread_deletions():
    for record in list_pending_deletions():
        guild = _resolve_guild(record["guild_id"])
        if guild is None:
            continue
        delete_after = datetime.fromisoformat(record["delete_after"])
        if delete_after <= datetime.now(delete_after.tzinfo):
            _launch(_delete_poll_thread_job(guild, record, _thread_delete_job_id(record)), "poll_thread_delete")
        else:
            schedule_poll_thread_deletion(record, guild, delete_after)


async def run_inactivity_nudges_for_guild(guild_id: str):
    settings = get_guild_settings(guild_id)
    if not settings["inactivity_nudge_scheduled"]:
        return
    groups = get_inactivity_nudge_groups(
        guild_id, settings["inactivity_nudge_days"], settings["inactivity_nudge_days"],
    )
    for group in groups:
        try:
            user = await client.fetch_user(int(group["discord_id"]))
            sent = await deliver_inactivity_nudge(
                user.send, group, embed=make_nudge_embed(group)
            )
            if not sent:
                print(f"INACTIVITY NUDGE FAILED: {group['discord_id']}")
        except Exception as exc:
            errors.log_exception(
                exc, surface="scheduled_job", job=INACTIVITY_NUDGE_JOB_ID,
                guild_id=guild_id, user=str(group.get("discord_id")),
            )


async def run_inactivity_nudges():
    for guild in client.guilds:
        try:
            await run_inactivity_nudges_for_guild(str(guild.id))
        except Exception as exc:
            errors.log_exception(
                exc, surface="scheduled_job", job=INACTIVITY_NUDGE_JOB_ID,
                guild_id=str(guild.id),
            )


async def run_checkin_prompts_for_guild(guild_id: str):
    guild = _resolve_guild(guild_id)
    if guild is None:
        return
    settings = get_guild_settings(guild_id)
    activity = get_weekly_activity(guild_id)
    prompt = await run_llm(llm.get_checkin_prompt, activity)
    channel = resolve_text_channel(guild, settings["checkin_prompt_channel"])
    if channel is None:
        return
    await send_summary(
        channel,
        "📖 Weekly check-in",
        prompt,
        kind="activity",
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def run_checkin_prompts():
    for guild in client.guilds:
        try:
            await run_checkin_prompts_for_guild(str(guild.id))
        except Exception as exc:
            errors.log_exception(exc, surface="scheduled_job", job=CHECKIN_PROMPT_JOB_ID, guild_id=str(guild.id))


async def run_weekly_recap_for_guild(guild_id: str):
    guild = _resolve_guild(guild_id)
    if guild is None:
        return
    settings = get_guild_settings(guild_id)
    activity = get_weekly_activity(guild_id)
    recap = await run_llm(llm.get_recap, activity)
    channel = resolve_text_channel(guild, settings["weekly_recap_channel"])
    if channel:
        await send_summary(
            channel,
            "📰 Weekly recap",
            recap,
            kind="activity",
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def run_weekly_recap():
    for guild in client.guilds:
        try:
            await run_weekly_recap_for_guild(str(guild.id))
        except Exception as exc:
            errors.log_exception(exc, surface="scheduled_job", job=WEEKLY_RECAP_JOB_ID, guild_id=str(guild.id))


async def run_weekly_post_for_guild(guild_id: str):
    guild = _resolve_guild(guild_id)
    if guild is None:
        return
    settings = get_guild_settings(guild_id)
    activity = get_weekly_activity(guild_id)
    post = await run_llm(llm.get_weekly_post, activity)
    channel = resolve_text_channel(guild, settings["weekly_recap_channel"])
    if channel:
        await send_summary(
            channel,
            "📰 This week in book club",
            post,
            kind="activity",
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def run_weekly_post():
    """Combined weekly recap + check-in in a single message."""
    for guild in client.guilds:
        try:
            await run_weekly_post_for_guild(str(guild.id))
        except Exception as exc:
            errors.log_exception(exc, surface="scheduled_job", job=WEEKLY_POST_JOB_ID, guild_id=str(guild.id))


def schedule_guild_jobs(guild_id: str) -> None:
    """Create or replace this guild's recap/check-in/nudge jobs from its settings row."""
    settings = get_guild_settings(guild_id)
    weekly_post_id = _guild_job_id(WEEKLY_POST_JOB_ID, guild_id)
    recap_id = _guild_job_id(WEEKLY_RECAP_JOB_ID, guild_id)
    checkin_id = _guild_job_id(CHECKIN_PROMPT_JOB_ID, guild_id)
    nudge_id = _guild_job_id(INACTIVITY_NUDGE_JOB_ID, guild_id)

    for job_id in (weekly_post_id, recap_id, checkin_id, nudge_id):
        _remove_job(job_id)

    if settings["combined_weekly_post"]:
        _add_cron_job(
            lambda gid=guild_id, jid=weekly_post_id: _launch(run_weekly_post_for_guild(gid), jid),
            weekly_post_id,
            settings["weekly_recap_day_of_week"],
            settings["weekly_recap_hour"],
        )
    else:
        if settings["weekly_recap_scheduled"]:
            _add_cron_job(
                lambda gid=guild_id, jid=recap_id: _launch(run_weekly_recap_for_guild(gid), jid),
                recap_id,
                settings["weekly_recap_day_of_week"],
                settings["weekly_recap_hour"],
            )
        if settings["checkin_prompt_scheduled"]:
            _add_cron_job(
                lambda gid=guild_id, jid=checkin_id: _launch(run_checkin_prompts_for_guild(gid), jid),
                checkin_id,
                settings["checkin_prompt_day_of_week"],
                settings["checkin_prompt_hour"],
            )

    if settings["inactivity_nudge_scheduled"]:
        nudge_job_id = nudge_id

        def job_fn(gid=guild_id, jid=nudge_job_id):
            _launch(run_inactivity_nudges_for_guild(gid), jid)

        scheduler.add_job(
            job_fn,
            "interval",
            hours=settings["inactivity_nudge_interval_hours"],
            id=nudge_job_id,
            replace_existing=True,
            misfire_grace_time=3600,
        )


def cancel_guild_jobs(guild_id: str) -> None:
    for prefix in (WEEKLY_POST_JOB_ID, WEEKLY_RECAP_JOB_ID, CHECKIN_PROMPT_JOB_ID, INACTIVITY_NUDGE_JOB_ID):
        _remove_job(_guild_job_id(prefix, guild_id))
    cancel_auto_close(guild_id)
    cancel_poll_auto_close(guild_id)
    cancel_coordination_poll_auto_close(guild_id)
    prefix = f"poll-thread-delete:{guild_id}:"
    for job_id, scheduler_id in list(poll_thread_deletion_jobs.items()):
        if job_id.startswith(prefix):
            _remove_job(scheduler_id)
            poll_thread_deletion_jobs.pop(job_id, None)


_READY_LOCK = None
_READY_LOCK_LOOP = None
_ENROLLMENT_SYNC_LOCKS: dict = {}
_ENROLLMENT_SYNC_LOCKS_LOOP = None
_ENROLLMENT_BUTTON_PREFIX = "enroll_"


def ready_lock() -> asyncio.Lock:
    """Process-wide lock so overlapping Discord on_ready events run one at a time."""
    global _READY_LOCK, _READY_LOCK_LOOP
    loop = asyncio.get_running_loop()
    if _READY_LOCK is None or _READY_LOCK_LOOP is not loop:
        _READY_LOCK = asyncio.Lock()
        _READY_LOCK_LOOP = loop
    return _READY_LOCK


def _enrollment_sync_lock(guild_id: str) -> asyncio.Lock:
    """Per-guild lock for the current event loop, so overlapping on_ready runs cannot double-post."""
    global _ENROLLMENT_SYNC_LOCKS, _ENROLLMENT_SYNC_LOCKS_LOOP
    loop = asyncio.get_running_loop()
    if _ENROLLMENT_SYNC_LOCKS_LOOP is not loop:
        _ENROLLMENT_SYNC_LOCKS = {}
        _ENROLLMENT_SYNC_LOCKS_LOOP = loop
    lock = _ENROLLMENT_SYNC_LOCKS.get(guild_id)
    if lock is None:
        lock = asyncio.Lock()
        _ENROLLMENT_SYNC_LOCKS[guild_id] = lock
    return lock


def _message_custom_ids(message) -> list[str]:
    ids = []
    for row in getattr(message, "components", None) or []:
        children = getattr(row, "children", None)
        if children is None:
            children = getattr(row, "components", None) or []
        for child in children:
            custom_id = getattr(child, "custom_id", None)
            if custom_id:
                ids.append(str(custom_id))
    return ids


def _is_persistent_enrollment_panel(message) -> bool:
    """Return whether a message is the standing enrollment panel, not a join ping."""
    if getattr(message, "mentions", None):
        return False
    bot_user = getattr(client, "user", None)
    author = getattr(message, "author", None)
    if bot_user is not None and author is not None and getattr(author, "id", None) != bot_user.id:
        return False
    if any(custom_id.startswith(_ENROLLMENT_BUTTON_PREFIX) for custom_id in _message_custom_ids(message)):
        return True
    embeds = getattr(message, "embeds", None) or []
    title = getattr(embeds[0], "title", None) if embeds else None
    return title == ENROLLMENT_PANEL_TITLE


async def _find_existing_enrollment_panel(channel):
    """Return the newest persistent enrollment panel in channel history, or None.

    discord.Forbidden / HTTPException propagate so the caller can refuse to post
    when it cannot tell whether a panel already exists.
    """
    try:
        history = channel.history(limit=100)
        async for message in history:
            if _is_persistent_enrollment_panel(message):
                return message
    except discord.NotFound:
        return None
    return None


async def _edit_enrollment_panel(message, view) -> bool:
    try:
        await message.edit(
            allowed_mentions=discord.AllowedMentions.none(),
            content=None,
            embed=make_enrollment_embed(),
            view=view,
        )
    except (discord.Forbidden, discord.HTTPException):
        return False
    return True


async def sync_enrollment_panel(guild_id: str) -> None:
    """Create, refresh, move, or retire a guild's persistent enrollment panel."""
    async with _enrollment_sync_lock(guild_id):
        await _sync_enrollment_panel(guild_id)


async def _sync_enrollment_panel(guild_id: str) -> None:
    from database import get_enrollment_panel, set_enrollment_message_id
    from views_enrollment import EnrollmentView

    guild = _resolve_guild(guild_id)
    if guild is None:
        return
    settings = get_guild_settings(guild_id)
    panel = get_enrollment_panel(guild_id)
    channel_ref = settings["opt_in_welcome_channel"]
    target = resolve_text_channel(guild, channel_ref)
    stored_channel = (
        resolve_text_channel(guild, panel["channel_id"]) if panel and panel["channel_id"] else None
    )
    # Pre-migration rows store message_id only; fall back to the current welcome channel.
    lookup_channel = stored_channel or target
    old_message = None
    if lookup_channel and panel and panel["message_id"]:
        try:
            old_message = await lookup_channel.fetch_message(int(panel["message_id"]))
        except discord.NotFound:
            old_message = None
        except (discord.Forbidden, discord.HTTPException):
            # A permission or transient API failure does not prove that the
            # stored panel is gone. Keep its ids and retry on the next sync
            # instead of sending another enrollment panel.
            return

    if not settings["member_opt_in_enabled"]:
        if old_message:
            try:
                await old_message.delete()
            except (discord.Forbidden, discord.HTTPException):
                pass
        set_enrollment_message_id(guild_id)
        return

    if target is None:
        return

    view = EnrollmentView()
    if old_message is not None and old_message.channel.id == target.id:
        if await _edit_enrollment_panel(old_message, view):
            set_enrollment_message_id(guild_id, old_message.id, target.id)
        return

    # Stored IDs 404 when the row is stale or the lookup channel is wrong, but
    # the live panel may still be in the welcome channel. Search recent history
    # before sending so restarts cannot stack duplicate panels.
    found = None
    history_error = False
    try:
        found = await _find_existing_enrollment_panel(target)
    except (discord.Forbidden, discord.HTTPException):
        history_error = True

    if found is not None:
        if old_message is not None and old_message.id != found.id:
            try:
                await old_message.delete()
            except (discord.Forbidden, discord.HTTPException):
                pass
        if await _edit_enrollment_panel(found, view):
            set_enrollment_message_id(guild_id, found.id, target.id)
        return

    if history_error:
        # Destination history is unreadable. Posting would risk a duplicate, so
        # keep the stored row (or the panel still sitting in another channel).
        # First-time guilds with no stored panel still post below.
        if old_message is not None or (panel and panel["message_id"]):
            return

    if old_message:
        try:
            await old_message.delete()
        except (discord.Forbidden, discord.HTTPException):
            pass
    try:
        message = await target.send(
            embed=make_enrollment_embed(),
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except (discord.Forbidden, discord.HTTPException):
        return
    set_enrollment_message_id(guild_id, message.id, target.id)


async def run_year_in_review_for_guild(guild, year: int) -> None:
    """Generate, send, and record one guild's year-in-review post."""
    guild_id = str(guild.id)
    settings = get_guild_settings(guild_id)
    activity = get_year_activity(guild_id)
    recap = await run_llm(llm.get_year_in_review, activity)
    channel = resolve_text_channel(guild, settings["year_in_review_channel"])
    if channel:
        await send_summary(
            channel,
            "🎆 Year in review",
            recap,
            kind="milestone",
            allowed_mentions=discord.AllowedMentions.none(),
        )
    log_event(None, guild_id, "year_in_review", {"year": year})


async def run_year_in_review():
    """Run year-in-review for all guilds, isolating failures by guild."""
    year = datetime.now().year
    for guild in client.guilds:
        try:
            await run_year_in_review_for_guild(guild, year)
        except Exception as exc:
            errors.log_exception(exc, surface="scheduled_job", job=YEAR_IN_REVIEW_JOB_ID, guild_id=str(guild.id))


def missed_year_in_review(guild_id: str, year: int) -> bool:
    """Returns True if year_in_review has NOT been run for this guild+year."""
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM event_log WHERE event_type = 'year_in_review' AND guild_id = ? AND json_extract(payload, '$.year') = ?",
            (guild_id, year),
        )
        return cur.fetchone() is None
    finally:
        conn.close()


async def recover_missed_year_in_review(now: datetime | None = None):
    """Recover a missed annual run only during its bounded startup grace window."""
    now = now or datetime.now()
    scheduled_at = now.replace(month=1, day=1, hour=9, minute=0, second=0, microsecond=0)
    if now < scheduled_at or now > scheduled_at + YEAR_IN_REVIEW_RECOVERY_GRACE:
        return
    for guild in client.guilds:
        if missed_year_in_review(str(guild.id), now.year):
            try:
                await run_year_in_review_for_guild(guild, now.year)
            except Exception as exc:
                errors.log_exception(exc, surface="scheduled_job", job=YEAR_IN_REVIEW_JOB_ID, guild_id=str(guild.id))


async def run_due_reminders(now: datetime | None = None) -> None:
    """DM due personal reminders and deactivate or reschedule each row."""
    now = now or datetime.now(timezone.utc)
    for reminder in list_due_reminders(now):
        try:
            user = await client.fetch_user(int(reminder["discord_id"]))
            await user.send(embed=make_reminder_embed(reminder))
        except (discord.Forbidden, discord.NotFound):
            mark_reminder_delivered(
                reminder["event_id"], reminder["recurrence"], reminder["scheduled_time"], now=now,
            )
            continue
        except Exception as exc:
            errors.log_exception(
                exc, surface="scheduled_job", job=REMINDER_SWEEP_JOB_ID,
                guild_id=str(reminder.get("guild_id")),
                user=str(reminder.get("discord_id")),
            )
            continue
        mark_reminder_delivered(
            reminder["event_id"], reminder["recurrence"], reminder["scheduled_time"], now=now,
        )


def schedule_reminder_sweep() -> None:
    scheduler.add_job(
        lambda: _launch(run_due_reminders(), REMINDER_SWEEP_JOB_ID),
        "interval",
        minutes=1,
        id=REMINDER_SWEEP_JOB_ID,
        replace_existing=True,
        misfire_grace_time=60,
    )


def schedule_year_in_review():
    if scheduler.get_job(YEAR_IN_REVIEW_JOB_ID):
        return

    def job_fn():
        _launch(run_year_in_review(), YEAR_IN_REVIEW_JOB_ID)

    scheduler.add_job(
        job_fn,
        "cron",
        month=1,
        day=1,
        hour=9,
        id=YEAR_IN_REVIEW_JOB_ID,
        replace_existing=True,
        misfire_grace_time=86400,
    )


def schedule_taste_profile_updates():
    if scheduler.get_job(TASTE_PROFILE_JOB_ID):
        return

    async def run_taste_profiles():
        for guild in client.guilds:
            try:
                if not get_guild_settings(str(guild.id))["taste_profile_scheduled"]:
                    continue
                users = get_all_active_users(str(guild.id))
            except Exception as exc:
                errors.log_exception(
                    exc, surface="scheduled_job", job=TASTE_PROFILE_JOB_ID,
                    guild_id=str(guild.id),
                )
                continue
            for user in users:
                try:
                    history = get_user_reading_history(user["discord_id"], str(guild.id))
                    if not history.get("finished"):
                        continue
                    profile = await run_llm(llm.get_taste_profile, history["finished"])
                    update_user_taste_profile(user["discord_id"], profile)
                except Exception as exc:
                    errors.log_exception(
                        exc, surface="scheduled_job", job=TASTE_PROFILE_JOB_ID,
                        guild_id=str(guild.id), user=str(user.get("discord_id")),
                    )

    def job_fn():
        _launch(run_taste_profiles(), TASTE_PROFILE_JOB_ID)

    scheduler.add_job(
        job_fn,
        "cron",
        month="1,4,7,10",
        day=1,
        hour=9,
        id=TASTE_PROFILE_JOB_ID,
        replace_existing=True,
        misfire_grace_time=86400,
    )


async def notify_admin(discord_client: discord.Client, content: str):
    if not ADMIN_DISCORD_ID:
        print(f"ADMIN ALERT: {content}")
        return

    try:
        admin_user = await discord_client.fetch_user(int(ADMIN_DISCORD_ID))
        await admin_user.send(content)
    except Exception as exc:
        print(f"ADMIN ALERT FAILED: {content}")
        errors.log_exception(exc, surface="admin_alert")


_last_llm_alert_at = {}


async def notify_llm_failure(failure: dict):
    """DM one actionable LLM-provider alert per provider/category cooldown window."""
    key = (failure["provider"], failure["category"])
    now = datetime.now()
    last_alert = _last_llm_alert_at.get(key)
    if last_alert and now - last_alert < timedelta(minutes=LLM_ALERT_COOLDOWN_MINUTES):
        return

    _last_llm_alert_at[key] = now
    status = f" (HTTP {failure['status_code']})" if failure.get("status_code") else ""
    await notify_admin(
        client,
        f"⚠️ LLM alert: {failure['provider']} returned a {failure['category']} error"
        f"{status}. Member requests are receiving the normal fallback message. "
        f"Further {failure['provider']} {failure['category']} alerts are suppressed for "
        f"{LLM_ALERT_COOLDOWN_MINUTES} minutes.",
    )


def schedule_llm_failure_alert(failure: dict):
    """Bridge synchronous LLM calls to the Discord event loop."""
    asyncio.create_task(notify_llm_failure(failure))
