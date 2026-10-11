"""Shared Discord orchestration for nomination windows and polls.

Natural-language handlers, slash commands, and the admin dashboard all call
these helpers so jobs, poll threads, public announcements, and tiebreakers
stay in one place. Callers still own admin checks and invoker-facing copy.
"""
from dataclasses import dataclass

import jobs
from actions import run_book_action
from actions_nominations import (
    close_coordination_poll,
    close_nomination_window,
    close_poll,
    get_active_coordination_poll,
    get_active_poll,
    get_active_window,
    open_coordination_poll,
    open_nomination_window,
    open_poll,
    open_tiebreaker_poll,
)
from embeds import make_poll_embed, make_summary_embed
from guild_settings import get_guild_settings
from helpers import channel_setting_label, resolve_text_channel
from poll_threads import (
    WORKFLOW_BOOK_POLL,
    WORKFLOW_COORDINATION,
    WORKFLOW_NOMINATIONS,
    WORKFLOW_TIEBREAKER,
    close_and_archive_poll_thread,
    create_poll_thread,
    get_poll_thread,
    resolve_poll_thread,
)
from actions_session import maybe_start_from_poll_winner
from sending import send_current_read, send_nominations_summary, send_poll_close, send_summary


@dataclass(frozen=True)
class PollWorkflowResult:
    ok: bool
    reason: str = ""
    channel_name: str = ""
    posted_elsewhere: bool = False


def _day_phrase(days: int) -> str:
    return f"{days} day" if days == 1 else f"{days} days"


def _duration(guild_id: str, days: int | None, key: str) -> int:
    return get_guild_settings(guild_id)[key] if days is None else days


def _nominations_channel(guild, guild_id: str):
    if not guild:
        return None
    return resolve_text_channel(guild, get_guild_settings(guild_id)["nominations_channel"])


def _nominations_channel_label(guild, guild_id: str) -> str:
    if not guild:
        return ""
    return channel_setting_label(
        get_guild_settings(guild_id)["nominations_channel"],
        guild,
        empty="",
    )


def _open_destination(guild, guild_id: str, fallback):
    named = _nominations_channel(guild, guild_id)
    destination = named or fallback
    posted_elsewhere = bool(
        named is not None and (fallback is None or getattr(fallback, "id", None) != named.id)
    )
    channel_name = _nominations_channel_label(guild, guild_id)
    return destination, channel_name, posted_elsewhere


async def _close_destination(guild, guild_id: str, record, fallback):
    named = _nominations_channel(guild, guild_id)
    thread = await resolve_poll_thread(guild, record) if record and guild else None
    return thread or named or fallback, named


def _ok(channel_name: str, posted_elsewhere: bool) -> PollWorkflowResult:
    return PollWorkflowResult(ok=True, channel_name=channel_name, posted_elsewhere=posted_elsewhere)


def _fail(reason: str) -> PollWorkflowResult:
    return PollWorkflowResult(ok=False, reason=reason)


def _register_persistent_view(view) -> None:
    add_view = getattr(getattr(jobs, "client", None), "add_view", None)
    if callable(add_view):
        add_view(view)


def _voting_view(guild_id: str, poll_id: int, nominations):
    from views_polls import VotingView
    return VotingView(guild_id, poll_id, nominations, timeout=None)


def _coordination_view(guild_id: str, poll_id: int, options):
    from views_polls import CoordinationView
    return CoordinationView(guild_id, poll_id, options, timeout=None)


def _nominate_view():
    from views_polls import NominateView
    return NominateView()


async def _archive_thread(guild, guild_id: str, workflow_type: str, record) -> None:
    if not guild or not record:
        return
    delete_after = await close_and_archive_poll_thread(
        guild, guild_id, workflow_type, record["workflow_id"]
    )
    if delete_after:
        jobs.schedule_poll_thread_deletion(record, guild, delete_after)


async def open_nominations_workflow(guild, guild_id: str, opened_by: str, days: int | None, fallback_channel) -> PollWorkflowResult:
    days = _duration(guild_id, days, "nominations_default_days")
    window_id = open_nomination_window(guild_id, opened_by, days=days)
    if not window_id:
        return _fail("already_open")
    window = get_active_window(guild_id)
    if window is None:
        return _fail("missing_record")
    if guild:
        jobs.schedule_auto_close(guild_id, window["closes_at"], guild)
    destination, channel_name, posted_elsewhere = _open_destination(guild, guild_id, fallback_channel)
    announcement = (
        f"Nominations are open! You have {_day_phrase(days)} "
        f"to nominate a book for our next read. Use `/nominate`, the Nominate button, "
        f"or say \"nominate [title] by [author] because [reason]\"."
    )
    view = _nominate_view()
    _register_persistent_view(view)
    if destination is not None:
        await create_poll_thread(
            destination, guild_id, WORKFLOW_NOMINATIONS, window_id, "🗳️ Nominations",
            "🗳️ Nominations open", announcement,
            make_summary_embed("🗳️ Nominations open", announcement, kind="poll"), view,
        )
    return _ok(channel_name, posted_elsewhere)


async def close_nominations_workflow(
    guild, guild_id: str, fallback_channel, *, manual: bool = True,
) -> PollWorkflowResult:
    active_window = get_active_window(guild_id)
    if not active_window:
        return _fail("not_open")
    record = get_poll_thread(guild_id, WORKFLOW_NOMINATIONS, active_window["window_id"])
    nominations = close_nomination_window(guild_id, manual=manual)
    jobs.cancel_auto_close(guild_id)
    destination, named = await _close_destination(guild, guild_id, record, fallback_channel)
    if destination is not None:
        await send_nominations_summary(destination, nominations)
    await _archive_thread(guild, guild_id, WORKFLOW_NOMINATIONS, record)
    channel_name = _nominations_channel_label(guild, guild_id)
    posted_elsewhere = bool(
        named is not None and (fallback_channel is None or getattr(fallback_channel, "id", None) != named.id)
    )
    return _ok(channel_name, posted_elsewhere)


async def open_book_poll_workflow(guild, guild_id: str, opened_by: str, days: int | None, fallback_channel) -> PollWorkflowResult:
    days = _duration(guild_id, days, "poll_default_days")
    if get_active_poll(guild_id):
        return _fail("already_open")
    poll_id, nominations = open_poll(guild_id, opened_by, days=days)
    poll_id = poll_id or 0
    if not poll_id:
        return _fail("no_nominations")
    poll = get_active_poll(guild_id)
    if poll is None:
        return _fail("missing_record")
    if guild:
        jobs.schedule_poll_auto_close(guild_id, poll["closes_at"], guild)
    view = _voting_view(guild_id, poll_id, nominations)
    _register_persistent_view(view)
    destination, channel_name, posted_elsewhere = _open_destination(guild, guild_id, fallback_channel)
    announcement = (
        f"Voting is now open! You have {_day_phrase(days)} "
        f"to vote on which book we should read next. Click a button below to cast your vote. "
        f"You can change your vote anytime before the poll closes."
    )
    if destination is not None:
        await create_poll_thread(
            destination, guild_id, WORKFLOW_BOOK_POLL, poll_id, "🗳️ Book poll",
            "🗳️ Voting open", announcement,
            make_poll_embed("🗳️ Voting open", announcement, nominations), view,
        )
    return _ok(channel_name, posted_elsewhere)


async def _open_tiebreaker(guild, guild_id: str, opened_by: str, tied_ids: list, parent) -> bool:
    days = get_guild_settings(guild_id)["poll_default_days"]
    poll_id, nominations = open_tiebreaker_poll(guild_id, opened_by, tied_ids, days=days)
    if not poll_id:
        return False
    poll = get_active_poll(guild_id)
    if poll is None:
        return False
    if guild:
        jobs.schedule_poll_auto_close(guild_id, poll["closes_at"], guild)
    view = _voting_view(guild_id, poll_id, nominations)
    _register_persistent_view(view)
    copy = "Vote again to choose the club's next read."
    destination = parent
    if destination is not None:
        await create_poll_thread(
            destination, guild_id, WORKFLOW_TIEBREAKER, poll_id, "⚖️ Tiebreaker poll",
            "⚖️ Tiebreaker poll", copy,
            make_poll_embed("⚖️ Tiebreaker poll", copy, nominations), view,
        )
    return True


async def close_book_poll_workflow(
    guild, guild_id: str, opened_by: str, fallback_channel, *, manual: bool = True,
) -> PollWorkflowResult:
    active_poll = get_active_poll(guild_id)
    if not active_poll:
        return _fail("not_open")
    record_type = WORKFLOW_TIEBREAKER if active_poll.get("is_tiebreaker") else WORKFLOW_BOOK_POLL
    record = get_poll_thread(guild_id, record_type, active_poll["poll_id"])
    result = close_poll(guild_id, manual=manual)
    jobs.cancel_poll_auto_close(guild_id)
    session_started = False
    if result.get("winner") and not result.get("tied"):
        session_started = await run_book_action(maybe_start_from_poll_winner,
            guild_id, opened_by, result["winner"], active_poll["poll_id"]
        ) is not None
    destination, named = await _close_destination(guild, guild_id, record, fallback_channel)
    if destination is not None:
        await send_poll_close(destination, result)
        if session_started:
            await send_current_read(destination, guild_id)
    await _archive_thread(guild, guild_id, record_type, record)
    parent = named or fallback_channel
    if result.get("tied") and not result.get("is_tiebreaker"):
        tied_ids = [nomination["nomination_id"] for nomination in result["tied_nominations"]]
        opened = await _open_tiebreaker(guild, guild_id, opened_by, tied_ids, parent)
        if not opened and destination is not None:
            await send_summary(
                destination, "⚖️ Tiebreaker unavailable",
                "The tiebreaker poll could not be opened. An admin must choose.",
                kind="poll",
            )
    elif result.get("tied") and parent is not None:
        await send_summary(
            parent, "⚖️ Tiebreaker unresolved",
            "It is still tied. An admin must choose the winner.",
            kind="poll",
        )
    channel_name = _nominations_channel_label(guild, guild_id)
    posted_elsewhere = bool(
        named is not None and (fallback_channel is None or getattr(fallback_channel, "id", None) != named.id)
    )
    return _ok(channel_name, posted_elsewhere)


async def open_coordination_poll_workflow(
    guild, guild_id: str, opened_by: str, question: str | None, options: list, days: int | None, fallback_channel,
) -> PollWorkflowResult:
    days = _duration(guild_id, days, "coordination_poll_default_days")
    question = (question or "").strip()
    option_list = [str(option).strip() for option in (options or []) if str(option).strip()]
    if not question or len(option_list) < 2 or len(option_list) > 25:
        return _fail("invalid_options")
    if get_active_coordination_poll(guild_id):
        return _fail("already_open")
    poll_id, option_rows = open_coordination_poll(guild_id, opened_by, question, option_list, days=days)
    poll_id = poll_id or 0
    if not poll_id:
        return _fail("already_open")
    poll = get_active_coordination_poll(guild_id)
    if poll is None:
        return _fail("missing_record")
    if guild:
        jobs.schedule_coordination_poll_auto_close(guild_id, poll["closes_at"], guild)
    view = _coordination_view(guild_id, poll_id, option_rows)
    _register_persistent_view(view)
    destination, channel_name, posted_elsewhere = _open_destination(guild, guild_id, fallback_channel)
    announcement = (
        f"**{question}**\n"
        f"Vote below — poll closes in {_day_phrase(days)}. You can change your vote any time before it closes."
    )
    if destination is not None:
        await create_poll_thread(
            destination, guild_id, WORKFLOW_COORDINATION, poll_id, "🗓️ Coordination poll",
            "🗓️ Coordination poll", announcement,
            make_poll_embed("🗓️ Coordination poll", announcement, option_rows), view,
        )
    return _ok(channel_name, posted_elsewhere)


async def close_coordination_poll_workflow(
    guild, guild_id: str, fallback_channel, *, manual: bool = True,
) -> PollWorkflowResult:
    active_poll = get_active_coordination_poll(guild_id)
    if not active_poll:
        return _fail("not_open")
    record = get_poll_thread(guild_id, WORKFLOW_COORDINATION, active_poll["poll_id"])
    result = close_coordination_poll(guild_id, manual=manual)
    jobs.cancel_coordination_poll_auto_close(guild_id)
    destination, named = await _close_destination(guild, guild_id, record, fallback_channel)
    if destination is not None:
        await send_poll_close(destination, result, coordination=True)
    await _archive_thread(guild, guild_id, WORKFLOW_COORDINATION, record)
    channel_name = _nominations_channel_label(guild, guild_id)
    posted_elsewhere = bool(
        named is not None and (fallback_channel is None or getattr(fallback_channel, "id", None) != named.id)
    )
    return _ok(channel_name, posted_elsewhere)
