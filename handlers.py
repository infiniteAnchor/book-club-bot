"""Message handler layer: RequestContext + every natural-language action handler.

Handlers receive a RequestContext (never raw (message, guild_id, ...) tuples),
the parsed intent payload (args, intent), and the mutable user_context used for
re-intent passes. The intent registry (handler + admin/guest flags) lives here too.
"""
import json
from dataclasses import dataclass
from typing import Any

import discord

import jobs
import llm
from llm import run as run_llm
from actions import (
    run_book_action,
    action_dnf_book,
    action_finish_book,
    action_group_query,
    action_rate_book,
    action_start_book,
    action_update_progress,
    get_access_tier,
    get_user_book_progress,
    get_user_reading_history,
    query_shelf,
)
from actions_nominations import (
    get_active_coordination_poll,
    get_active_poll,
    get_active_window,
    get_coordination_poll_results,
    get_nominations,
    get_poll_results,
    replace_nomination,
    submit_nomination,
)
from actions_queue import (
    action_queue_add,
    action_queue_remove,
    action_wishlist_add,
    action_wishlist_remove,
    list_next_book_choices,
)
from actions_session import (
    complete_club_read,
    set_club_target,
    start_club_read,
)
from actions_social import (
    TRACKING_FEATURE,
    action_set_goal,
    action_set_reminder,
    check_achievements,
    clear_user_tone_override,
    get_goal_progress,
    get_club_history,
    get_leaderboard_stats,
    TIER_PARTICIPATION_ARGS,
    describe_participation,
    get_member_feature_preferences,
    get_nomination_context_for_book,
    get_streak_leaderboard,
    get_streaks,
    get_weekly_activity,
    get_year_activity,
    has_club,
    list_user_tone_overrides,
    set_user_tone_override,
)
from config import (
    CONVERSATIONAL_RESPONSES,
    DEBUG_INTENT,
    NOMINATIONS_ADMIN_ROLE,
)
from guild_settings import default_settings, get_guild_settings
from embeds import make_intent_embed, make_list_embeds, make_summary_embed, send_embeds
from helpers import (
    build_spoiler_thread_name,
    channel_setting_label,
    find_user_by_name,
    has_admin_role,
    resolve_text_channel,
    typing_indicator,
)
from help_content import HELP_TOPICS, build_help_embed
from actions_scrapbook import add_scrapbook_entry, get_scrapbook, remove_scrapbook_entry
from views_scrapbook import ADD_ERRORS, RevealScrapbookView
from poll_workflows import (
    close_book_poll_workflow,
    close_coordination_poll_workflow,
    close_nominations_workflow,
    open_book_poll_workflow,
    open_coordination_poll_workflow,
    open_nominations_workflow,
)
from participation_roles import ParticipationRoleError, set_participation
from sending import (
    CLUB_REQUIRED_NOMINATE,
    INCOMPLETE_ACTION_MESSAGE,
    INCOMPLETE_ACTION_SHORT,
    NOMINATION_NO_WINDOW,
    QUEUE_LIST_PUBLIC_ACK,
    announce_achievements,
    send_current_read,
    send_notice,
    send_summary,
)
from views import (
    BookActionView,
    OfferNextBookView,
    ReadingListView,
    RevealPrivateListView,
    SaveBookDestinationView,
    build_admin_dashboard_embed,
)


@dataclass(frozen=True)
class RequestContext:
    """Immutable per-message context handed to every handler.

    user_context (the LLM context dict) is deliberately kept OUT of this object:
    handlers mutate it during re-intent passes.
    """
    message: discord.Message
    guild_id: str
    author_id: str
    display_name: str
    reply_channel: Any


def _settings(ctx: RequestContext) -> dict:
    return get_guild_settings(ctx.guild_id)


def build_member_help_embed(member, guild_id: str | None, topic: str | None, *, is_dm: bool = False, guild=None):
    """Build help from the same authorization and enrollment rules as chat actions."""
    is_admin = bool(member and not is_dm and has_admin_role(member))
    settings = get_guild_settings(guild_id) if guild_id else default_settings()
    guild_obj = guild or getattr(member, "guild", None)
    welcome_channel = channel_setting_label(
        settings["opt_in_welcome_channel"],
        guild_obj,
        empty="#book-bot",
    )
    is_enrolled = not settings["member_opt_in_enabled"]
    if guild_id and not is_dm:
        preferences = get_member_feature_preferences(str(member.id), guild_id)
        is_enrolled = preferences[TRACKING_FEATURE]
    return build_help_embed(
        topic,
        is_admin=is_admin,
        is_enrolled=is_enrolled,
        is_dm=is_dm,
        welcome_channel=welcome_channel,
    )


async def send_member_help(channel, member, guild_id: str | None, topic: str | None = None, *, is_dm: bool = False, ephemeral: bool = False):
    embed = build_member_help_embed(member, guild_id, topic, is_dm=is_dm)
    if ephemeral:
        return await channel.response.send_message(embed=embed, ephemeral=True)
    else:
        return await channel.send(embed=embed)


async def _send_response(ctx: RequestContext, intent):
    if CONVERSATIONAL_RESPONSES:
        return await send_embeds(
            ctx.reply_channel,
            [make_intent_embed(intent.get("action", "unknown"), intent["response"], intent.get("args"))],
        )
    else:
        return await ctx.reply_channel.send(f"```json\n{json.dumps(intent, indent=2)}\n```")


async def _handle_help(ctx: RequestContext, args, _intent, _ctx):
    topic = args.get("topic")
    if topic not in HELP_TOPICS:
        topic = None
    await send_member_help(
        ctx.reply_channel,
        ctx.message.author,
        ctx.guild_id if ctx.message.guild else None,
        topic,
        is_dm=ctx.message.guild is None,
    )


async def _notice(ctx: RequestContext, description: str, *, title: str = "📚 Book Club", kind: str = "info"):
    return await send_notice(ctx.reply_channel, description, title=title, kind=kind)


async def _incomplete(ctx: RequestContext, *, short: bool = False):
    text = INCOMPLETE_ACTION_SHORT if short else INCOMPLETE_ACTION_MESSAGE
    return await _notice(ctx, text)


async def _require_admin(ctx: RequestContext):
    await _notice(ctx, f"Only members with the '{NOMINATIONS_ADMIN_ROLE}' role can do that.")


# -- Simple write actions (add_book, start_book, add_to_wishlist, update_progress) --

async def _handle_simple(ctx: RequestContext, args, intent, _ctx):
    action_name = intent["action"]
    if action_name in {"add_to_wishlist", "add_book"}:
        ok, status = await run_book_action(action_wishlist_add,
            ctx.author_id, ctx.display_name, ctx.guild_id, args, source="chat"
        )
        if status == "full":
            await _notice(
                ctx,
                f"Your wishlist is full (max 25). Remove something before adding "
                f"**{args.get('title') or 'that book'}**.",
                title="✨ Wishlist",
            )
            return
        if status == "missing_title" or not ok:
            await _incomplete(ctx)
            return
        await _send_response(ctx, intent)
        return
    fn = {
        "start_book": action_start_book,
        "update_progress": action_update_progress,
    }[action_name]
    ok = await run_book_action(fn, ctx.author_id, ctx.display_name, ctx.guild_id, args)
    if not ok:
        await _incomplete(ctx)
        return
    if CONVERSATIONAL_RESPONSES and action_name in {"start_book", "update_progress"} and args.get("title"):
        view = BookActionView(
            ctx.author_id, ctx.guild_id, args["title"], args.get("author"),
            can_progress=True, can_finish=True,
        )
        await send_embeds(
            ctx.reply_channel,
            [make_intent_embed(action_name, intent["response"], args)], view=view,
        )
        return
    await _send_response(ctx, intent)


def _next_book_view(ctx: RequestContext):
    """Attach a pick-next control when the member has queue or wishlist items."""
    if list_next_book_choices(ctx.author_id, ctx.guild_id):
        return OfferNextBookView(ctx.author_id, ctx.guild_id)
    return None


# -- finish_book (simple + achievements) --

async def _handle_finish_book(ctx: RequestContext, args, intent, _ctx):
    ok = await run_book_action(action_finish_book, ctx.author_id, ctx.display_name, ctx.guild_id, args)
    if ok:
        new_ach = check_achievements(ctx.author_id, ctx.display_name, ctx.guild_id)
        if new_ach:
            await announce_achievements(ctx.reply_channel, ctx.display_name, new_ach)
    if not ok:
        await _incomplete(ctx)
        return
    if CONVERSATIONAL_RESPONSES and args.get("title"):
        view = BookActionView(
            ctx.author_id,
            ctx.guild_id,
            args["title"],
            args.get("author"),
            can_rate=True,
            offer_next=bool(_next_book_view(ctx)),
        )
        await send_embeds(
            ctx.reply_channel,
            [make_intent_embed("finish_book", intent["response"], args)],
            view=view,
        )
        return
    await _send_response(ctx, intent)


# -- rate_book --

async def _handle_rate_book(ctx: RequestContext, args, intent, user_context):
    ok = await run_book_action(action_rate_book, ctx.author_id, ctx.display_name, args, ctx.guild_id)
    if ok:
        new_ach = check_achievements(ctx.author_id, ctx.display_name, ctx.guild_id)
        if new_ach:
            await announce_achievements(ctx.reply_channel, ctx.display_name, new_ach)
        rated_title = args.get("title")
        rated_author = args.get("author")
        nomination_context = get_nomination_context_for_book(
            ctx.author_id, rated_title, ctx.guild_id, rated_author
        ) if rated_title else {}
        if nomination_context:
            user_context["rated_book"] = {
                "title": rated_title, "author": rated_author,
                "score": args.get("score") or args.get("rating"),
            }
            user_context["nomination_reason"] = nomination_context.get("nomination_reason")
            user_context["nomination_context"] = nomination_context
            async with typing_indicator(ctx.reply_channel):
                new_intent = await run_llm(llm.get_intent, ctx.message.content, user_context)
            if DEBUG_INTENT:
                print(f"\n--- Rate Book re-intent ---")
                print(json.dumps(new_intent, indent=2))
                print("---")
            intent.update(new_intent)
    if not ok:
        await _incomplete(ctx)
        return
    await _send_response(ctx, intent)


# -- dnf_book --

async def _handle_dnf_book(ctx: RequestContext, args, intent, user_context):
    success, dnf_title, dnf_author = await run_book_action(action_dnf_book,
        ctx.author_id, ctx.display_name, ctx.guild_id, args
    )
    if success:
        new_ach = check_achievements(ctx.author_id, ctx.display_name, ctx.guild_id)
        eulogy = await run_llm(llm.get_dnf_eulogy, dnf_title, dnf_author, user_context.get("books", []))
        view = _next_book_view(ctx)
        await send_embeds(
            ctx.reply_channel,
            [make_intent_embed("dnf_book", eulogy, {"title": dnf_title, "author": dnf_author})],
            view=view,
        )
        if new_ach:
            await announce_achievements(ctx.reply_channel, ctx.display_name, new_ach)
    else:
        await _incomplete(ctx)


# -- private queue + wishlist remove --

async def _handle_queue_add(ctx: RequestContext, args, intent, _ctx):
    ok, status = await run_book_action(action_queue_add,
        ctx.author_id, ctx.display_name, ctx.guild_id, args, source="chat"
    )
    if status == "missing_title" or (not ok and status not in {"full"}):
        await _incomplete(ctx)
        return
    if status == "full":
        await _notice(
            ctx,
            f"Your private queue is full (max 25). Remove something before adding "
            f"**{args.get('title') or 'that book'}**.",
            title="🔒 Private queue",
        )
        return
    if status == "already":
        await send_embeds(
            ctx.reply_channel,
            [make_intent_embed("queue_add", f"{args.get('title')} is already on your private queue.", args)],
        )
        return
    await _send_response(ctx, intent)


async def _handle_queue_remove(ctx: RequestContext, args, intent, _ctx):
    ok, status = await run_book_action(action_queue_remove, ctx.author_id, ctx.display_name, ctx.guild_id, args)
    if status == "missing_title":
        await _incomplete(ctx)
        return
    if status == "not_found":
        title = args.get("title") or "That book"
        await _notice(
            ctx,
            f"**{title}** wasn't on your private queue.",
            title="🔒 Private queue",
        )
        return
    if not ok:
        await _incomplete(ctx)
        return
    await _send_response(ctx, intent)


async def _handle_queue_list(ctx: RequestContext, args, intent, _ctx):
    view = RevealPrivateListView(ctx.author_id, ctx.guild_id)
    await send_embeds(
        ctx.reply_channel,
        [make_intent_embed("queue_list", QUEUE_LIST_PUBLIC_ACK, args)],
        view=view,
    )


async def _handle_export_shelf(ctx: RequestContext, _args, _intent, _ctx):
    # The CSV includes private queue titles, so chat never carries it; /export replies ephemerally.
    if ctx.message.guild is None:
        text = "Run `/export` in your book-club server to download your shelf there as a CSV."
    else:
        text = (
            "Run `/export` to download your shelf in this server as a CSV for Goodreads, "
            "StoryGraph, or Hardcover. Only you will see the file."
        )
    await _notice(ctx, text, title="📤 Shelf export")


async def _handle_remove_from_wishlist(ctx: RequestContext, args, intent, _ctx):
    ok, status = await run_book_action(action_wishlist_remove, ctx.author_id, ctx.display_name, ctx.guild_id, args)
    if status == "missing_title":
        await _incomplete(ctx)
        return
    if status == "not_found":
        title = args.get("title") or "That book"
        await _notice(
            ctx,
            f"**{title}** wasn't on your wishlist.",
            title="✨ Wishlist",
        )
        return
    if not ok:
        await _incomplete(ctx)
        return
    await _send_response(ctx, intent)


# -- set_goal --

async def _handle_set_goal(ctx: RequestContext, args, intent, user_context):
    ok = action_set_goal(ctx.author_id, ctx.display_name, args)
    if ok:
        user_context["reading_goal"] = get_goal_progress(ctx.author_id, ctx.guild_id)
        async with typing_indicator(ctx.reply_channel):
            new_intent = await run_llm(llm.get_intent, ctx.message.content, user_context)
        if DEBUG_INTENT:
            print(f"\n--- Set Goal re-intent ---")
            print(json.dumps(new_intent, indent=2))
            print("---")
        intent.update(new_intent)
    if not ok:
        await _incomplete(ctx)
        return
    await _send_response(ctx, intent)


# -- query (self + @mention + re-intent) --

async def _handle_query(ctx: RequestContext, args, intent, user_context):
    shelf = args.get("shelf", "reading")
    if shelf in ("want_to_read", "wishlist", "want-to-read"):
        shelf = "wishlist"
    elif shelf in ("finished", "done", "completed", "read"):
        shelf = "finished"
    else:
        shelf = "reading"
    target_id = ctx.author_id
    target_name = ctx.display_name
    if ctx.message.mentions:
        for mentioned in ctx.message.mentions:
            if mentioned != jobs.client.user:
                target_id = str(mentioned.id)
                target_name = str(mentioned.display_name)
                break
    if target_id == ctx.author_id:
        target_user_name = args.get("target_user") or args.get("user")
        if target_user_name:
            found_id, found_name = find_user_by_name(target_user_name, ctx.guild_id)
            if found_id and found_name:
                target_id = found_id
                target_name = found_name
    books = query_shelf(target_id, shelf, ctx.guild_id)
    user_context["shelf"] = shelf
    user_context["books_on_shelf"] = books
    user_context["queried_user"] = target_name
    async with typing_indicator(ctx.reply_channel):
        new_intent = await run_llm(llm.get_intent, ctx.message.content, user_context)
    if DEBUG_INTENT:
        print(f"\n--- Query re-intent ---")
        print(json.dumps(new_intent, indent=2))
        print("---")
    if not isinstance(new_intent.get("response"), str) or not new_intent["response"].strip():
        await _incomplete(ctx)
        return
    items = [
        {"title": book.get("title"), "author": book.get("author"), "detail": book.get("status") or shelf}
        for book in books
    ]
    pages = make_list_embeds(f"📚 {target_name}'s {shelf.replace('_', ' ')} shelf", items, kind="info", empty=new_intent["response"])
    pages[0].description = new_intent["response"][:4096]
    view = None
    if (target_id == ctx.author_id and shelf == "reading" and books):
        view = ReadingListView(ctx.author_id, ctx.guild_id, books)
    message = await send_embeds(ctx.reply_channel, pages, invoker_id=ctx.message.author.id, view=view)
    # Store message and embed references for inline action editing
    if view and message:
        view.message = message
        view.original_embed = pages[0]


# -- group_query (re-intent) --

async def _handle_group_query(ctx: RequestContext, _args, _intent, user_context):
    reading_data = action_group_query(ctx.guild_id)
    user_context["group_reading"] = reading_data
    async with typing_indicator(ctx.reply_channel):
        new_intent = await run_llm(llm.get_intent, ctx.message.content, user_context)
    if DEBUG_INTENT:
        print(f"\n--- Group Query re-intent ---")
        print(json.dumps(new_intent, indent=2))
        print("---")
    if not isinstance(new_intent.get("response"), str) or not new_intent["response"].strip():
        await _incomplete(ctx)
        return
    await _send_response(ctx, new_intent)


# -- summarize, year_in_review --

async def _handle_summarize(ctx: RequestContext, _args, _intent, _ctx):
    activity = get_weekly_activity(ctx.guild_id)
    recap = await run_llm(llm.get_recap, activity)
    channel = (
        resolve_text_channel(ctx.message.guild, _settings(ctx)["weekly_recap_channel"])
        if ctx.message.guild else None
    )
    if channel:
        await send_summary(channel, "📰 Weekly recap", recap, kind="activity")
    else:
        await send_summary(ctx.reply_channel, "📰 Weekly recap", recap, kind="activity")


async def _handle_year_in_review(ctx: RequestContext, _args, _intent, _ctx):
    activity = get_year_activity(ctx.guild_id)
    recap = await run_llm(llm.get_year_in_review, activity)
    channel = (
        resolve_text_channel(ctx.message.guild, _settings(ctx)["year_in_review_channel"])
        if ctx.message.guild else None
    )
    if channel:
        await send_summary(channel, "🎆 Year in review", recap, kind="milestone")
    else:
        await send_summary(ctx.reply_channel, "🎆 Year in review", recap, kind="milestone")


# -- leaderboard --

async def _handle_leaderboard(ctx: RequestContext, _args, _intent, _ctx):
    stats = get_leaderboard_stats(ctx.guild_id)
    if not stats:
        await send_embeds(
            ctx.reply_channel,
            make_list_embeds(
                "🏆 Club leaderboard",
                [],
                kind="milestone",
                empty="No reading stats yet — finish some books first!",
            ),
        )
        return
    narrative = await run_llm(llm.get_leaderboard_narrative, stats)
    items = [
        {"name": row["name"], "detail": f"{row['books_finished']} finished • {row['pages_read']} pages"}
        for row in stats
    ]
    pages = make_list_embeds("🏆 Club leaderboard", items, kind="milestone")
    pages[0].description = narrative[:4096]
    await send_embeds(ctx.reply_channel, pages, invoker_id=ctx.message.author.id)

# -- club_history --

async def _handle_club_history(ctx: RequestContext, _args, _intent, _ctx):
    history = get_club_history(ctx.guild_id)
    if not history:
        await send_embeds(
            ctx.reply_channel,
            make_list_embeds(
                "📚 Club history",
                [],
                kind="milestone",
                empty="The club hasn't finished any books yet.",
            ),
        )
        return
    items = []
    for row in history:
        detail = f"{row['finishes']} finished"
        if row.get("finishers"):
            detail += f" • {row['finishers']}"
        if row.get("avg_score") is not None:
            detail += f" • ⭐ {row['avg_score']}/10"
        items.append({"title": row["title"], "author": row.get("author"), "detail": detail})
    narrative = await run_llm(llm.get_club_history_narrative, history)
    pages = make_list_embeds("📚 Club history", items, kind="milestone")
    pages[0].description = narrative[:4096]
    await send_embeds(ctx.reply_channel, pages, invoker_id=ctx.message.author.id)


async def _handle_scrapbook(ctx: RequestContext, args, _intent, _ctx):
    if not ctx.message.guild:
        await _notice(ctx, "Use scrapbook in your book-club server.")
        return
    sessions = args.get("operation") == "sessions"
    selection = {"sessions": True} if sessions else {}
    if not sessions:
        data = get_scrapbook(ctx.guild_id, session_id=args.get("session_id"), title=args.get("title"))
        if data is None:
            await _notice(ctx, ADD_ERRORS["no_session"])
            return
        # Bind the button to this read even if an admin starts another meanwhile.
        selection["session_id"] = data["session"]["session_id"]
    await send_embeds(
        ctx.reply_channel,
        [make_intent_embed("scrapbook", "Open the club scrapbook privately below. It may contain spoilers.")],
        view=RevealScrapbookView(int(ctx.author_id), ctx.guild_id, **selection),
    )


async def _handle_scrapbook_add(ctx: RequestContext, args, _intent, _ctx):
    if not ctx.message.guild:
        await _notice(ctx, "Use scrapbook in your book-club server.")
        return
    ok, status, entry = add_scrapbook_entry(ctx.guild_id, ctx.author_id, ctx.display_name, args)
    if not ok:
        await _notice(ctx, ADD_ERRORS.get(status, "I couldn't save that moment."))
        return
    # Like the private queue, never echo model-authored text or args publicly.
    await _notice(ctx, f"Saved scrapbook entry #{entry['entry_id']}. Club readers can open it with /scrapbook.")


async def _handle_scrapbook_remove(ctx: RequestContext, args, _intent, _ctx):
    if not ctx.message.guild:
        await _notice(ctx, "Use scrapbook in your book-club server.")
        return
    removed = remove_scrapbook_entry(ctx.guild_id, ctx.author_id, args.get("entry_id"))
    await _notice(ctx, "Your scrapbook entry was removed." if removed else "No entry of yours matches that number in this server.")


# -- check_goal --

async def _handle_check_goal(ctx: RequestContext, _args, _intent, user_context):
    progress = get_goal_progress(ctx.author_id, ctx.guild_id)
    if not progress:
        await _notice(
            ctx,
            "You haven't set a reading goal yet. Try 'I want to read 12 books this year'.",
            title="🎯 Reading goal",
            kind="milestone",
        )
        return
    user_context["reading_goal"] = progress
    async with typing_indicator(ctx.reply_channel):
        new_intent = await run_llm(llm.get_intent, ctx.message.content, user_context)
    if not isinstance(new_intent.get("response"), str) or not new_intent["response"].strip():
        await _incomplete(ctx)
        return
    await _send_response(ctx, new_intent)


# -- admin_dashboard --

async def _handle_admin_dashboard(ctx: RequestContext, _args, _intent, _ctx):
    if not has_admin_role(ctx.message.author):
        await _require_admin(ctx)
        return
    await ctx.reply_channel.send(
        embed=build_admin_dashboard_embed(ctx.guild_id, guild=ctx.message.guild)
    )


# -- set_reminder, streak, streak_leaderboard --

async def _handle_set_reminder(ctx: RequestContext, args, _intent, _ctx):
    result = action_set_reminder(ctx.author_id, ctx.display_name, ctx.guild_id, args)
    await send_embeds(ctx.reply_channel, [make_intent_embed("set_reminder", result, args)])


async def _handle_streak(ctx: RequestContext, _args, _intent, _ctx):
    s = get_streaks(ctx.author_id, ctx.guild_id)
    if s["current"] > 0:
        embed = make_summary_embed("🔥 Reading streak", f"You're on a **{s['current']}-day** reading streak! Your longest streak is **{s['longest']} days**. Keep it up!", kind="milestone")
        embed.add_field(name="Current", value=f"{s['current']} days", inline=True)
        embed.add_field(name="Longest", value=f"{s['longest']} days", inline=True)
        await send_embeds(ctx.reply_channel, [embed])
    else:
        await _notice(
            ctx,
            "You don't have an active reading streak yet. Log some progress to start one!",
            title="🔥 Reading streak",
            kind="milestone",
        )


async def _handle_streak_leaderboard(ctx: RequestContext, _args, _intent, _ctx):
    streaks = get_streak_leaderboard(ctx.guild_id)
    if not streaks:
        await send_embeds(
            ctx.reply_channel,
            make_list_embeds(
                "🔥 Reading streak leaderboard",
                [],
                kind="milestone",
                empty="No active streaks yet. Start reading to claim the top spot!",
            ),
            invoker_id=ctx.message.author.id,
        )
        return
    items = [
        {"name": f"{i}. {s['display_name']}", "detail": f"{s['current']} day streak (best: {s['longest']})"}
        for i, s in enumerate(streaks, 1)
    ]
    await send_embeds(ctx.reply_channel, make_list_embeds("🔥 Reading streak leaderboard", items, kind="milestone"), invoker_id=ctx.message.author.id)


# -- tone overrides --

async def _handle_set_tone(ctx: RequestContext, args, _intent, _ctx):
    if not has_admin_role(ctx.message.author):
        await _notice(ctx, "Only admins can set tone overrides.", title="🎭 Tone overrides")
        return
    target_name = args.get("user")
    mode = args.get("mode", "neutral")
    if not target_name or ctx.message.guild is None:
        await _notice(ctx, "I need a user name and a mode: `glaze`, `roast`, or `neutral`.", title="🎭 Tone overrides")
        return
    found = discord.utils.get(ctx.message.guild.members, display_name=target_name) or \
            discord.utils.get(ctx.message.guild.members, name=target_name) or \
            discord.utils.get(ctx.message.guild.members, nick=target_name)
    if not found:
        await _notice(ctx, f"Couldn't find user **{target_name}** in this server.", title="🎭 Tone overrides")
        return
    result = set_user_tone_override(str(found.id), found.display_name, ctx.message.guild and str(ctx.message.guild.id), mode, ctx.display_name)
    await _notice(ctx, result, title="🎭 Tone overrides")


async def _handle_clear_tone(ctx: RequestContext, args, _intent, _ctx):
    if not has_admin_role(ctx.message.author):
        await _notice(ctx, "Only admins can clear tone overrides.", title="🎭 Tone overrides")
        return
    target_name = args.get("user")
    if not target_name or ctx.message.guild is None:
        await _notice(ctx, "I need a user name.", title="🎭 Tone overrides")
        return
    found = discord.utils.get(ctx.message.guild.members, display_name=target_name) or \
            discord.utils.get(ctx.message.guild.members, name=target_name) or \
            discord.utils.get(ctx.message.guild.members, nick=target_name)
    if not found:
        await _notice(ctx, f"Couldn't find user **{target_name}** in this server.", title="🎭 Tone overrides")
        return
    gid = str(ctx.message.guild.id) if ctx.message.guild else None
    result = clear_user_tone_override(str(found.id), gid)
    await _notice(ctx, result, title="🎭 Tone overrides")


async def _handle_list_tones(ctx: RequestContext, _args, _intent, _ctx):
    if not has_admin_role(ctx.message.author):
        await _notice(ctx, "Only admins can list tone overrides.", title="🎭 Tone overrides")
        return
    gid = str(ctx.message.guild.id) if ctx.message.guild else None
    tones = list_user_tone_overrides(gid)
    items = [
        {"name": t["display_name"], "detail": f"{t['mode']} (set by: {t['set_by']})"}
        for t in tones
    ]
    await send_embeds(
        ctx.reply_channel,
        make_list_embeds("🎭 Tone overrides", items, kind="info", empty="No active tone overrides."),
    )


# -- nominations --

async def _handle_open_nominations(ctx: RequestContext, args, _intent, _ctx):
    if not has_admin_role(ctx.message.author):
        await _require_admin(ctx)
        return
    days = args.get("days")
    try:
        days = int(days) if days is not None else _settings(ctx)["nominations_default_days"]
    except (TypeError, ValueError):
        days = _settings(ctx)["nominations_default_days"]
    result = await open_nominations_workflow(
        ctx.message.guild, ctx.guild_id, ctx.author_id, days, ctx.reply_channel,
    )
    if not result.ok:
        await _notice(
            ctx,
            "There's already a nomination window open for this server. Close it first before opening a new one.",
            title="🗳️ Nominations",
            kind="poll",
        )
        return
    if result.posted_elsewhere:
        await _notice(ctx, f"Nominations opened in {result.channel_name}.", title="🗳️ Nominations", kind="poll")


async def _handle_nominate(ctx: RequestContext, args, intent, _ctx):
    if _settings(ctx)["member_opt_in_enabled"] and not has_club(ctx.author_id, ctx.guild_id):
        await _notice(
            ctx,
            CLUB_REQUIRED_NOMINATE,
            title="🗳️ Nominations",
            kind="poll",
        )
        return
    title = args.get("title")
    author = args.get("author")
    reason = args.get("reason")
    replace = bool(args.get("replace"))
    if not title:
        await _notice(
            ctx,
            "I need a book title to nominate. Try: \"nominate [title] by [author] because [reason]\".",
            title="🗳️ Nominations",
            kind="poll",
        )
        return
    if replace:
        ok = await run_book_action(replace_nomination, ctx.author_id, ctx.guild_id, title, author, reason)
        if ok:
            await send_embeds(
                ctx.reply_channel,
                [make_intent_embed("list_nominations", intent["response"], args)],
                view=_nomination_save_view(ctx, title, author),
            )
        else:
            await _notice(
                ctx,
                "I couldn't replace your nomination — either there's no active window or you haven't nominated yet.",
                title="🗳️ Nominations",
                kind="poll",
            )
        return
    success, msg = await run_book_action(submit_nomination, ctx.author_id, ctx.display_name, ctx.guild_id, title, author, reason)
    if success:
        await send_embeds(
            ctx.reply_channel,
            [make_intent_embed("list_nominations", intent["response"], args)],
            view=_nomination_save_view(ctx, title, author),
        )
    elif msg == "no_window":
        await _notice(
            ctx,
            NOMINATION_NO_WINDOW,
            title="🗳️ Nominations",
            kind="poll",
        )
    elif msg == "already_nominated":
        await _notice(
            ctx,
            "You've already nominated a book in this round. If you'd like to change your pick, "
            "say \"replace my nomination with [title] by [author] because [reason]\".",
            title="🗳️ Nominations",
            kind="poll",
        )
    else:
        await _notice(ctx, "Something went wrong submitting that nomination.", title="🗳️ Nominations", kind="poll")


async def _handle_close_nominations(ctx: RequestContext, _args, _intent, _ctx):
    if not has_admin_role(ctx.message.author):
        await _require_admin(ctx)
        return
    result = await close_nominations_workflow(ctx.message.guild, ctx.guild_id, ctx.reply_channel)
    if not result.ok:
        await _notice(ctx, "There's no nomination window open to close.", title="🗳️ Nominations", kind="poll")
        return
    if result.posted_elsewhere:
        await _notice(
            ctx,
            f"Nominations closed. Summary posted in {result.channel_name}.",
            title="🗳️ Nominations",
            kind="poll",
        )


async def _handle_list_nominations(ctx: RequestContext, _args, _intent, user_context):
    noms = get_nominations(ctx.guild_id)
    user_context["nominations"] = noms
    user_context["active_window"] = get_active_window(ctx.guild_id)
    async with typing_indicator(ctx.reply_channel):
        new_intent = await run_llm(llm.get_intent, ctx.message.content, user_context)
    if DEBUG_INTENT:
        print(f"\n--- List nominations re-intent ---")
        print(json.dumps(new_intent, indent=2))
        print("---")
    if not isinstance(new_intent.get("response"), str) or not new_intent["response"].strip():
        await _incomplete(ctx, short=True)
        return
    items = [
        {
            "title": nomination.get("title"),
            "author": nomination.get("author"),
            "detail": f"Nominated by {nomination.get('display_name', 'a club member')}" +
                      (f" — {nomination['reason']}" if nomination.get("reason") else ""),
        }
        for nomination in noms
    ]
    pages = make_list_embeds("🗳️ Nominations", items, kind="poll", empty=new_intent["response"])
    pages[0].description = new_intent["response"][:4096]
    await send_embeds(ctx.reply_channel, pages, invoker_id=ctx.message.author.id)
    save_view = _nominations_save_view(ctx, noms)
    if save_view:
        await send_embeds(
            ctx.reply_channel,
            [make_intent_embed(
                "list_nominations",
                "Want to keep one? Pick a nomination, then save it to your private queue or public wishlist.",
                {},
            )],
            view=save_view,
        )


# -- polls --

async def _handle_open_poll(ctx: RequestContext, args, _intent, _ctx):
    if not has_admin_role(ctx.message.author):
        await _require_admin(ctx)
        return
    days = args.get("days")
    try:
        days = int(days) if days is not None else _settings(ctx)["poll_default_days"]
    except (TypeError, ValueError):
        days = _settings(ctx)["poll_default_days"]
    result = await open_book_poll_workflow(
        ctx.message.guild, ctx.guild_id, ctx.author_id, days, ctx.reply_channel,
    )
    if not result.ok:
        await _notice(
            ctx,
            "A poll is already open, or there are no nominations to vote on.",
            title="🗳️ Poll results",
            kind="poll",
        )
        return
    if result.posted_elsewhere:
        await _notice(ctx, f"Poll opened in {result.channel_name}.", title="🗳️ Poll results", kind="poll")


async def _handle_close_poll(ctx: RequestContext, _args, _intent, _ctx):
    if not has_admin_role(ctx.message.author):
        await _require_admin(ctx)
        return
    result = await close_book_poll_workflow(
        ctx.message.guild, ctx.guild_id, ctx.author_id, ctx.reply_channel,
    )
    if not result.ok:
        await _notice(ctx, "There's no active poll to close.", title="🗳️ Poll results", kind="poll")
        return


async def _handle_poll_results(ctx: RequestContext, _args, _intent, user_context):
    results = get_poll_results(ctx.guild_id)
    user_context["poll_results"] = results
    user_context["active_poll"] = get_active_poll(ctx.guild_id)
    async with typing_indicator(ctx.reply_channel):
        new_intent = await run_llm(llm.get_intent, ctx.message.content, user_context)
    if DEBUG_INTENT:
        print(f"\n--- Poll results re-intent ---")
        print(json.dumps(new_intent, indent=2))
        print("---")
    if not isinstance(new_intent.get("response"), str) or not new_intent["response"].strip():
        await _incomplete(ctx, short=True)
        return
    items = [{**entry["nomination"], "vote_count": entry["vote_count"]} for entry in results]
    pages = make_list_embeds("🗳️ Poll results", items, kind="poll", empty=new_intent["response"])
    pages[0].description = new_intent["response"][:4096]
    await send_embeds(ctx.reply_channel, pages, invoker_id=ctx.message.author.id)


# -- open_thread --

async def _handle_open_thread(ctx: RequestContext, args, intent, _ctx):
    title = args.get("title")
    author = args.get("author")
    topic = args.get("topic") or args.get("reason")
    if not title:
        await _notice(ctx, "I need a book title before I can open a spoiler-safe thread.", title="🗣️ Spoiler thread", kind="activity")
        return
    progress = get_user_book_progress(ctx.author_id, title, ctx.guild_id, author)
    if not progress or progress.get("status") != "finished":
        await _notice(
            ctx,
            f"You need to finish **{title}** before I can open a spoiler-safe thread for it.",
            title="🗣️ Spoiler thread",
            kind="activity",
        )
        return
    thread_name = build_spoiler_thread_name(progress["title"], progress.get("author"))
    anchor = await send_summary(ctx.reply_channel, "🗣️ Opening spoiler thread", f"Opening a spoiler-safe thread for **{progress['title']}**...", kind="activity")
    create_thread = getattr(anchor, "create_thread", None)
    if not create_thread:
        await _notice(ctx, "I couldn't create a thread here with this Discord library.", title="🗣️ Spoiler thread", kind="activity")
        return
    try:
        thread = await create_thread(name=thread_name, auto_archive_duration=_settings(ctx)["thread_auto_archive_minutes"])
    except TypeError:
        thread = await create_thread(name=thread_name)
    kickoff = f"Spoilers for **{progress['title']}** are allowed here."
    if topic:
        kickoff += f"\nTopic: {topic}"
    await send_summary(thread, "⚠️ Spoiler-safe discussion", kickoff, kind="activity")
    await send_embeds(ctx.reply_channel, [make_intent_embed("open_thread", f"{intent['response']} {thread.mention}", args)])


# -- coordination polls --

async def _handle_open_coordination_poll(ctx: RequestContext, args, _intent, _ctx):
    if not has_admin_role(ctx.message.author):
        await _require_admin(ctx)
        return
    question = args.get("question")
    options = args.get("options", [])
    days = args.get("days")
    try:
        days = int(days) if days is not None else _settings(ctx)["coordination_poll_default_days"]
    except (TypeError, ValueError):
        days = _settings(ctx)["coordination_poll_default_days"]
    result = await open_coordination_poll_workflow(
        ctx.message.guild, ctx.guild_id, ctx.author_id, question, options, days, ctx.reply_channel,
    )
    if not result.ok:
        if result.reason == "invalid_options":
            await _notice(
                ctx,
                "I need a question and 2–25 options to open a coordination poll.",
                title="🗓️ Coordination poll",
                kind="poll",
            )
            return
        await _notice(
            ctx,
            "A coordination poll is already open. Close it before opening a new one.",
            title="🗓️ Coordination poll",
            kind="poll",
        )
        return
    if result.posted_elsewhere:
        await _notice(
            ctx,
            f"Coordination poll opened in {result.channel_name}.",
            title="🗓️ Coordination poll",
            kind="poll",
        )


async def _handle_close_coordination_poll(ctx: RequestContext, _args, _intent, _ctx):
    if not has_admin_role(ctx.message.author):
        await _require_admin(ctx)
        return
    result = await close_coordination_poll_workflow(ctx.message.guild, ctx.guild_id, ctx.reply_channel)
    if not result.ok:
        await _notice(ctx, "There's no active coordination poll to close.", title="🗓️ Coordination poll", kind="poll")
        return
    if result.posted_elsewhere:
        await _notice(
            ctx,
            f"Poll closed. Results posted in {result.channel_name}.",
            title="🗓️ Coordination poll",
            kind="poll",
        )


async def _handle_coordination_poll_results(ctx: RequestContext, _args, _intent, user_context):
    results = get_coordination_poll_results(ctx.guild_id)
    user_context["coordination_poll_results"] = results
    user_context["active_coordination_poll"] = get_active_coordination_poll(ctx.guild_id)
    async with typing_indicator(ctx.reply_channel):
        new_intent = await run_llm(llm.get_intent, ctx.message.content, user_context)
    if not isinstance(new_intent.get("response"), str) or not new_intent["response"].strip():
        await _incomplete(ctx, short=True)
        return
    pages = make_list_embeds("🗓️ Coordination poll results", results, kind="poll", empty=new_intent["response"])
    pages[0].description = new_intent["response"][:4096]
    await send_embeds(ctx.reply_channel, pages, invoker_id=ctx.message.author.id)


# -- recommend --

async def _handle_recommend(ctx: RequestContext, args, _intent, _ctx):
    history = get_user_reading_history(ctx.author_id, ctx.guild_id)
    payload = await run_llm(llm.get_recommendation, ctx.message.content, args, history)
    text = llm.format_recommendation_text(payload)
    view = None
    books = payload.get("books") or []
    if books:
        view = SaveBookDestinationView(
            ctx.author_id, ctx.guild_id, books, source="recommendation"
        )
    await send_embeds(
        ctx.reply_channel,
        [make_intent_embed("recommend", text or "Here are a few ideas.", args)],
        view=view,
    )


def _nomination_save_view(ctx: RequestContext, title: str | None, author: str | None = None):
    if not title:
        return None
    return SaveBookDestinationView(
        ctx.author_id,
        ctx.guild_id,
        [{"title": title, "author": author}],
        source="nomination",
    )


def _nominations_save_view(ctx: RequestContext, noms: list):
    books = [
        {"title": n.get("title"), "author": n.get("author")}
        for n in noms
        if n.get("title")
    ]
    if not books:
        return None
    return SaveBookDestinationView(
        ctx.author_id, ctx.guild_id, books, source="nomination"
    )


def _truthy_flag(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "replace"}
    return bool(value)


async def _handle_current_read(ctx: RequestContext, _args, _intent, _ctx):
    await send_current_read(ctx.reply_channel, ctx.guild_id)


async def _handle_manage_club_read(ctx: RequestContext, args, _intent, _ctx):
    if not has_admin_role(ctx.message.author):
        await _require_admin(ctx)
        return
    operation = str(args.get("operation") or "").strip().lower()
    if operation in {"start", "replace"}:
        replace = operation == "replace" or _truthy_flag(args.get("replace"))
        ok, status, session = await run_book_action(start_club_read,
            ctx.guild_id, ctx.author_id, args, replace=replace
        )
        if status == "missing_title":
            await _incomplete(ctx)
            return
        if status == "invalid_date":
            await _notice(ctx, "I need a target date like `2026-10-15`, or `clear` to remove one.")
            return
        if status == "already_active" and session:
            title = session.get("title") or "the current club book"
            await _notice(
                ctx,
                f"The club is already reading **{title}**. Complete it first, or ask me to replace it.",
            )
            return
        if not ok:
            await _incomplete(ctx)
            return
        await send_current_read(ctx.reply_channel, ctx.guild_id)
        return
    if operation in {"complete", "finish", "close"}:
        ok, status, _session = complete_club_read(ctx.guild_id, ctx.author_id)
        if status == "no_session":
            await _notice(ctx, "There is no current club book to complete.")
            return
        if not ok:
            await _incomplete(ctx)
            return
        await _notice(ctx, "The club's current book is complete.", title="📖 Club reading session", kind="success")
        return
    if operation in {"set_target", "target"}:
        target = args.get("target_date", args.get("date", args.get("target")))
        ok, status, _session = set_club_target(ctx.guild_id, ctx.author_id, target)
        if status == "no_session":
            await _notice(ctx, "Start a current club book before setting a target date.")
            return
        if status == "invalid_date":
            await _notice(ctx, "I need a target date like `2026-10-15`, or `clear` to remove one.")
            return
        if not ok:
            await _incomplete(ctx)
            return
        await send_current_read(ctx.reply_channel, ctx.guild_id)
        return
    await _notice(
        ctx,
        "I can start, replace, complete, or set a target date for the club's current book.",
    )


async def _handle_manage_membership(ctx: RequestContext, args, _intent, _ctx):
    """Manage only the authoring member's preferences in the current guild."""
    if not ctx.guild_id:
        await send_embeds(
            ctx.reply_channel,
            [make_intent_embed(
                "manage_membership",
                "To join the book club, use the enrollment buttons in "
                f"{channel_setting_label(_settings(ctx)['opt_in_welcome_channel'], ctx.message.guild, empty='#welcome')} "
                "in your server.",
                args,
            )],
        )
        return

    operation = args.get("operation", "show")
    try:
        if operation == "show":
            preferences = get_member_feature_preferences(ctx.author_id, ctx.guild_id)
        elif operation == "join":
            preferences = await _write_participation(
                ctx,
                **TIER_PARTICIPATION_ARGS.get(
                    args.get("tier") or "tracking",
                    TIER_PARTICIPATION_ARGS["tracking"],
                ),
            )
        elif operation in ("opt_out", "enable_nudges", "disable_nudges"):
            changes = {
                "opt_out": {"tracking": False, "dm_nudges": False},
                "enable_nudges": {"dm_nudges": True},
                "disable_nudges": {"dm_nudges": False},
            }
            preferences = await _write_participation(ctx, **changes[operation])
        else:
            await send_embeds(
                ctx.reply_channel,
                [make_intent_embed(
                    "manage_membership",
                    "I can help you join or leave the book club, change your tier, or turn reading nudge DMs on or off.",
                    args,
                )],
            )
            return
    except ParticipationRoleError as exc:
        await send_embeds(
            ctx.reply_channel,
            [make_intent_embed("manage_membership", str(exc), args)],
        )
        return

    reply = describe_participation(preferences)
    if get_access_tier(ctx.author_id) == "guest" and preferences[TRACKING_FEATURE]:
        reply += " Your guest access tier can still restrict writing and voting actions."
    await send_embeds(ctx.reply_channel, [make_intent_embed("manage_membership", reply, args)])


async def _write_participation(ctx: RequestContext, **kwargs):
    """Resolve the live guild member and update Discord plus SQLite together."""
    member = ctx.message.author if ctx.message.guild else None
    if member is None and jobs.client is not None:
        guild = jobs.client.get_guild(int(ctx.guild_id))
        if guild is not None:
            member = guild.get_member(int(ctx.author_id))
            if member is None:
                try:
                    member = await guild.fetch_member(int(ctx.author_id))
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    member = None
    if member is None:
        raise ParticipationRoleError(
            "I couldn't find your live server membership to update its Discord roles."
        )
    return await set_participation(member, **kwargs)


# ── Intent registry ──────────────────────────────────────────────────────────
# Add new chat actions here AND in llm.VALID_ACTIONS.
# admin_only: skip member opt-in (handlers still enforce has_admin_role).
# guest_blocked: write/vote actions denied for access_tier == guest.

@dataclass(frozen=True)
class IntentAction:
    handler: Any
    admin_only: bool = False
    guest_blocked: bool = False


INTENT_ACTIONS = {
    "add_book":                  IntentAction(_handle_simple, guest_blocked=True),
    "start_book":                IntentAction(_handle_simple, guest_blocked=True),
    "add_to_wishlist":           IntentAction(_handle_simple, guest_blocked=True),
    "remove_from_wishlist":      IntentAction(_handle_remove_from_wishlist, guest_blocked=True),
    "queue_add":                 IntentAction(_handle_queue_add, guest_blocked=True),
    "queue_remove":              IntentAction(_handle_queue_remove, guest_blocked=True),
    "queue_list":                IntentAction(_handle_queue_list),
    "export_shelf":              IntentAction(_handle_export_shelf, guest_blocked=True),
    "update_progress":           IntentAction(_handle_simple, guest_blocked=True),
    "finish_book":               IntentAction(_handle_finish_book, guest_blocked=True),
    "rate_book":                 IntentAction(_handle_rate_book, guest_blocked=True),
    "dnf_book":                  IntentAction(_handle_dnf_book, guest_blocked=True),
    "set_goal":                  IntentAction(_handle_set_goal, guest_blocked=True),
    "query":                     IntentAction(_handle_query),
    "group_query":               IntentAction(_handle_group_query),
    "summarize":                 IntentAction(_handle_summarize),
    "year_in_review":            IntentAction(_handle_year_in_review),
    "leaderboard":               IntentAction(_handle_leaderboard),
    "club_history":              IntentAction(_handle_club_history),
    "scrapbook":                 IntentAction(_handle_scrapbook, guest_blocked=True),
    "scrapbook_add":             IntentAction(_handle_scrapbook_add, guest_blocked=True),
    "scrapbook_remove":          IntentAction(_handle_scrapbook_remove, guest_blocked=True),
    "check_goal":                IntentAction(_handle_check_goal),
    "admin_dashboard":           IntentAction(_handle_admin_dashboard, admin_only=True),
    "set_reminder":              IntentAction(_handle_set_reminder),
    "streak":                    IntentAction(_handle_streak),
    "streak_leaderboard":        IntentAction(_handle_streak_leaderboard),
    "set_tone":                  IntentAction(_handle_set_tone, admin_only=True),
    "clear_tone":                IntentAction(_handle_clear_tone, admin_only=True),
    "list_tones":                IntentAction(_handle_list_tones, admin_only=True),
    "open_nominations":          IntentAction(_handle_open_nominations, admin_only=True),
    "nominate":                  IntentAction(_handle_nominate, guest_blocked=True),
    "close_nominations":         IntentAction(_handle_close_nominations, admin_only=True),
    "list_nominations":          IntentAction(_handle_list_nominations),
    "open_poll":                 IntentAction(_handle_open_poll, admin_only=True),
    "close_poll":                IntentAction(_handle_close_poll, admin_only=True),
    "poll_results":              IntentAction(_handle_poll_results),
    "open_thread":               IntentAction(_handle_open_thread, guest_blocked=True),
    "open_coordination_poll":    IntentAction(_handle_open_coordination_poll, admin_only=True),
    "close_coordination_poll":   IntentAction(_handle_close_coordination_poll, admin_only=True),
    "coordination_poll_results": IntentAction(_handle_coordination_poll_results),
    "recommend":                 IntentAction(_handle_recommend),
    "manage_membership":         IntentAction(_handle_manage_membership),
    "help":                      IntentAction(_handle_help),
    "current_read":              IntentAction(_handle_current_read),
    "manage_club_read":          IntentAction(_handle_manage_club_read, admin_only=True),
    "unknown":                   IntentAction(None),  # handled in bot.py before dispatch
}

_ACTION_HANDLERS = {name: spec.handler for name, spec in INTENT_ACTIONS.items()}
ADMIN_ONLY_ACTIONS = frozenset(
    name for name, spec in INTENT_ACTIONS.items() if spec.admin_only
)
GUEST_BLOCKED_ACTIONS = frozenset(
    name for name, spec in INTENT_ACTIONS.items() if spec.guest_blocked
)
OPT_IN_EXEMPT_ACTIONS = ADMIN_ONLY_ACTIONS | {"manage_membership", "help", "current_read"}


def plan_dispatch(intents: list[dict]) -> list[dict]:
    """Order of intents to dispatch for one message; [] means stay silent.

    silent items are dropped. unknown items only survive when nothing actionable
    was parsed, and then only the first, so the member gets one "didn't catch
    that" reply rather than one per fragment.
    """
    spoken = [intent for intent in intents if intent.get("action") != "silent"]
    actionable = [intent for intent in spoken if intent.get("action") != "unknown"]
    return actionable or spoken[:1]


def describe_dispatch(intent: dict) -> dict:
    """Routing facts for one planned intent (dry-run harness and debug output)."""
    spec = INTENT_ACTIONS.get(intent.get("action"))
    handler = spec.handler if spec else None
    return {
        "handler": getattr(handler, "__name__", None),
        "admin_only": bool(spec and spec.admin_only),
        "guest_blocked": bool(spec and spec.guest_blocked),
        "opt_in_exempt": intent.get("action") in OPT_IN_EXEMPT_ACTIONS,
    }
