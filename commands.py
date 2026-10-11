"""Slash command layer: /help, /admin-dashboard, /nominations, /nominate, /poll, /coordination, /queue, /current, /history, /scrapbook, /import, /export, /status.

Commands are plain functions (with describe/choices metadata applied at
definition time) and are registered onto the app-command tree by register(tree),
called from bot.py once the tree exists. This avoids importing bot.py.
"""
from io import BytesIO

import discord

from actions import get_access_tier, run_book_action
from actions_import import (
    EXPORT_FILENAME,
    MAX_FILE_BYTES,
    build_shelf_export,
    existing_shelf_titles,
    parse_goodreads_csv,
)
from actions_queue import action_queue_add, action_queue_remove
from actions_session import complete_club_read, get_current_read, set_club_target, start_club_read
from actions_social import get_club_history, has_tracking
from embeds import PagedEmbedView, build_current_read_embed, make_list_embeds
from guild_settings import get_guild_settings
from handlers import send_member_help
from helpers import has_admin_role
from views_polls import (
    NOMINATION_AUTHOR_MAX_LENGTH,
    NOMINATION_REASON_MAX_LENGTH,
    NOMINATION_TITLE_MAX_LENGTH,
    submit_nomination_interaction,
)
from sending import send_current_read
from views_import import (
    EXPORT_GUEST_MESSAGE,
    EXPORT_TRACKING_MESSAGE,
    IMPORT_GUEST_MESSAGE,
    IMPORT_TRACKING_MESSAGE,
    GoodreadsImportView,
    build_export_embed,
    build_import_preview_embed,
)
from views_queue import private_queue_payload
from actions_scrapbook import add_scrapbook_entry, remove_scrapbook_entry
from views_scrapbook import ADD_ERRORS, scrapbook_access_ok, send_scrapbook
from poll_workflows import (
    close_book_poll_workflow,
    close_coordination_poll_workflow,
    close_nominations_workflow,
    open_book_poll_workflow,
    open_coordination_poll_workflow,
    open_nominations_workflow,
)
from views import (
    AdminDashboardView,
    build_admin_dashboard_embed,
)


# Discord caps slash-command and option descriptions at 100 characters.
DISCORD_DESCRIPTION_MAX = 100

COMMAND_DESCRIPTIONS = {
    "help": "Browse the club guide by topic, with chat examples for books, polls, stats, and more",
    "status": "Private health check: uptime, version, database size, Discord latency, and LLM provider",
    "admin-dashboard": "Private admin panel: server settings, roles, polls, coordination, tone",
    "nominations": "Collect book ideas for the next club read: open, close, or list nominations",
    "nominate": "Submit or replace your book nomination (Club tier; needs an open window)",
    "poll": "Vote on nominated books for the next club read: open, close, or view results",
    "coordination": "Vote on schedule or pacing: open, close, or view a coordination poll",
    "queue": "Private TBR: list, add, or remove books. The list is visible only to you",
    "current": "Show the club's current book, or start, set a target, or complete it",
    "history": "Browse the club's finished books: recent-first archive with ratings",
    "scrapbook": "Save and browse club quotes, highlights, and memories privately (may contain spoilers)",
    "import": "Upload a Goodreads library CSV to add your finished books here",
    "export": "Download your shelf as a CSV you can import into Goodreads or StoryGraph",
}

OPTION_DESCRIPTIONS = {
    "help": {
        "topic": "Jump to a help category: books, club, polls, stats, membership, or admin",
    },
    "nominations": {
        "action": "Open or close the window (admins), or list current nominations (anyone)",
        "days": "How many days nominations stay open (1-365). Used only with Open",
    },
    "nominate": {
        "title": "Book title to nominate",
        "author": "Optional author",
        "reason": "Optional reason for the club",
    },
    "poll": {
        "action": "Open or close the book vote (admins), or view current results (anyone)",
        "days": "How many days voting stays open (1-365). Used only with Open",
    },
    "coordination": {
        "action": "Open or close the poll (admins), or view current results (anyone)",
        "question": "The question members vote on. Required when opening a poll",
        "options": "Vote choices, one per line. Needs 2-25 options when opening",
        "days": "How many days the poll stays open (1-365). Used only with Open",
    },
    "queue": {
        "action": "List your private queue, or add/remove a book",
        "title": "Book title. Required when adding or removing",
        "author": "Optional author, used when adding or removing",
    },
    "current": {
        "action": "Show the current book, or start, set a target, or complete it",
        "title": "Book title. Required when starting a club read",
        "author": "Optional author, used when starting",
        "date": "Target date (YYYY-MM-DD). Used when starting or setting a target",
    },
    "import": {
        "file": "Goodreads export CSV from My Books → Import and export",
    },
}


@discord.app_commands.describe(topic=OPTION_DESCRIPTIONS["help"]["topic"])
@discord.app_commands.choices(topic=[
    discord.app_commands.Choice(name="Books", value="books"),
    discord.app_commands.Choice(name="Club activity", value="club"),
    discord.app_commands.Choice(name="Nominations and polls", value="polls"),
    discord.app_commands.Choice(name="Goals and stats", value="stats"),
    discord.app_commands.Choice(name="Membership and nudges", value="membership"),
    discord.app_commands.Choice(name="Admin tools", value="admin"),
])
async def slash_help(interaction: discord.Interaction, topic: discord.app_commands.Choice[str] | None = None):
    """Slash command that shares its output with natural-language help."""
    guild_id = str(interaction.guild_id) if interaction.guild_id else None
    await send_member_help(
        interaction,
        interaction.user,
        guild_id,
        topic.value if topic else None,
        is_dm=interaction.guild is None,
        ephemeral=interaction.guild is not None,
    )


async def slash_status(interaction: discord.Interaction):
    """Show operational health — uptime, version, DB size, latency, LLM."""
    import status as status_helpers

    latency_raw = None
    try:
        # interaction.client is the discord.Client; latency is float seconds or nan
        latency_raw = getattr(interaction.client, "latency", None)
        if latency_raw is None and hasattr(interaction, "client"):
            # Fallback: try jobs.client (set by bot.py) if interaction.client is unavailable
            import jobs as jobs_module

            latency_raw = getattr(getattr(jobs_module, "client", None), "latency", None)
    except Exception:
        latency_raw = None

    embed = status_helpers.build_status_embed(
        uptime=status_helpers.get_uptime_str(),
        db_size=status_helpers.get_db_size_str(),
        latency=status_helpers.format_latency(latency_raw),
        llm=status_helpers.get_llm_display(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def slash_admin_dashboard(interaction: discord.Interaction):
    """Open the Python-owned admin dashboard without an LLM intent call."""
    if not interaction.guild or not has_admin_role(interaction.user):
        await interaction.response.send_message("Only server admins can open the club dashboard.", ephemeral=True)
        return
    guild_id = str(interaction.guild.id)
    view = AdminDashboardView(guild_id, interaction.user.id, guild=interaction.guild)
    await interaction.response.send_message(
        embed=build_admin_dashboard_embed(guild_id, guild=interaction.guild),
        view=view,
        ephemeral=True,
    )
    view.message = await interaction.original_response()


def _valid_command_days(days: int | None, default: int) -> int | None:
    """Return a safe command duration, or None when the supplied value is invalid."""
    value = default if days is None else days
    return value if 1 <= value <= 365 else None


async def _require_slash_admin(interaction: discord.Interaction) -> bool:
    if not interaction.guild:
        await interaction.response.send_message("Use this command in your book-club server.", ephemeral=True)
        return False
    if not has_admin_role(interaction.user):
        await interaction.response.send_message("Only server admins can run that command.", ephemeral=True)
        return False
    return True


@discord.app_commands.describe(
    action=OPTION_DESCRIPTIONS["nominations"]["action"],
    days=OPTION_DESCRIPTIONS["nominations"]["days"],
)
@discord.app_commands.choices(action=[
    discord.app_commands.Choice(name="Open nominations", value="open"),
    discord.app_commands.Choice(name="Close nominations", value="close"),
    discord.app_commands.Choice(name="List nominations", value="list"),
])
async def slash_nominations(
    interaction: discord.Interaction,
    action: discord.app_commands.Choice[str],
    days: int | None = None,
):
    """Direct command path for the nomination window lifecycle."""
    if not interaction.guild:
        await interaction.response.send_message("Use this command in your book-club server.", ephemeral=True)
        return
    guild_id = str(interaction.guild.id)
    if action.value == "list":
        await interaction.response.send_message(embed=build_admin_dashboard_embed(guild_id, "nominations"), ephemeral=True)
        return
    if not await _require_slash_admin(interaction):
        return
    if action.value == "open":
        duration = _valid_command_days(days, get_guild_settings(guild_id)["nominations_default_days"])
        if duration is None:
            await interaction.response.send_message("Duration must be between 1 and 365 days.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        result = await open_nominations_workflow(
            interaction.guild, guild_id, str(interaction.user.id), duration, interaction.channel,
        )
        if not result.ok:
            await interaction.followup.send("A nomination window is already open.", ephemeral=True)
            return
        await interaction.followup.send("Nominations are open; the announcement has been posted.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    result = await close_nominations_workflow(interaction.guild, guild_id, interaction.channel)
    if not result.ok:
        await interaction.followup.send("There is no nomination window open to close.", ephemeral=True)
        return
    await interaction.followup.send("Nominations closed; the summary has been posted.", ephemeral=True)


@discord.app_commands.describe(
    title=OPTION_DESCRIPTIONS["nominate"]["title"],
    author=OPTION_DESCRIPTIONS["nominate"]["author"],
    reason=OPTION_DESCRIPTIONS["nominate"]["reason"],
)
async def slash_nominate(
    interaction: discord.Interaction,
    title: discord.app_commands.Range[str, 1, NOMINATION_TITLE_MAX_LENGTH],
    author: discord.app_commands.Range[str, 1, NOMINATION_AUTHOR_MAX_LENGTH] | None = None,
    reason: discord.app_commands.Range[str, 1, NOMINATION_REASON_MAX_LENGTH] | None = None,
):
    """Member path to submit or replace a nomination without using chat."""
    await submit_nomination_interaction(interaction, title, author, reason)


@discord.app_commands.describe(
    action=OPTION_DESCRIPTIONS["poll"]["action"],
    days=OPTION_DESCRIPTIONS["poll"]["days"],
)
@discord.app_commands.choices(action=[
    discord.app_commands.Choice(name="Open book poll", value="open"),
    discord.app_commands.Choice(name="Close book poll", value="close"),
    discord.app_commands.Choice(name="View results", value="results"),
])
async def slash_poll(
    interaction: discord.Interaction,
    action: discord.app_commands.Choice[str],
    days: int | None = None,
):
    """Direct command path for book-poll administration and results."""
    if not interaction.guild:
        await interaction.response.send_message("Use this command in your book-club server.", ephemeral=True)
        return
    guild_id = str(interaction.guild.id)
    if action.value == "results":
        await interaction.response.send_message(embed=build_admin_dashboard_embed(guild_id, "book_poll"), ephemeral=True)
        return
    if not await _require_slash_admin(interaction):
        return
    if action.value == "open":
        duration = _valid_command_days(days, get_guild_settings(guild_id)["poll_default_days"])
        if duration is None:
            await interaction.response.send_message("Duration must be between 1 and 365 days.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        result = await open_book_poll_workflow(
            interaction.guild, guild_id, str(interaction.user.id), duration, interaction.channel,
        )
        if not result.ok:
            await interaction.followup.send(
                "A poll is already open, or there are no closed nominations to vote on.",
                ephemeral=True,
            )
            return
        await interaction.followup.send("The book poll is open and ready for votes.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    result = await close_book_poll_workflow(
        interaction.guild, guild_id, str(interaction.user.id), interaction.channel,
    )
    if not result.ok:
        await interaction.followup.send("There is no book poll open to close.", ephemeral=True)
        return
    await interaction.followup.send("The book poll has been closed; results were posted.", ephemeral=True)


@discord.app_commands.describe(
    action=OPTION_DESCRIPTIONS["coordination"]["action"],
    question=OPTION_DESCRIPTIONS["coordination"]["question"],
    options=OPTION_DESCRIPTIONS["coordination"]["options"],
    days=OPTION_DESCRIPTIONS["coordination"]["days"],
)
@discord.app_commands.choices(action=[
    discord.app_commands.Choice(name="Open scheduling poll", value="open"),
    discord.app_commands.Choice(name="Close scheduling poll", value="close"),
    discord.app_commands.Choice(name="View results", value="results"),
])
async def slash_coordination(
    interaction: discord.Interaction,
    action: discord.app_commands.Choice[str],
    question: str | None = None,
    options: str | None = None,
    days: int | None = None,
):
    """Direct command path for coordination-poll administration and results."""
    if not interaction.guild:
        await interaction.response.send_message("Use this command in your book-club server.", ephemeral=True)
        return
    guild_id = str(interaction.guild.id)
    if action.value == "results":
        await interaction.response.send_message(embed=build_admin_dashboard_embed(guild_id, "coordination"), ephemeral=True)
        return
    if not await _require_slash_admin(interaction):
        return
    if action.value == "open":
        duration = _valid_command_days(days, get_guild_settings(guild_id)["coordination_poll_default_days"])
        option_list = [value.strip() for value in (options or "").splitlines() if value.strip()]
        if duration is None:
            await interaction.response.send_message("Duration must be between 1 and 365 days.", ephemeral=True)
            return
        if not question or len(option_list) < 2 or len(option_list) > 25:
            await interaction.response.send_message("Opening a coordination poll needs a question and 2–25 newline-separated options.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        result = await open_coordination_poll_workflow(
            interaction.guild, guild_id, str(interaction.user.id),
            question.strip(), option_list, duration, interaction.channel,
        )
        if not result.ok:
            if result.reason == "invalid_options":
                await interaction.followup.send(
                    "Opening a coordination poll needs a question and 2–25 newline-separated options.",
                    ephemeral=True,
                )
                return
            await interaction.followup.send("A coordination poll is already open.", ephemeral=True)
            return
        await interaction.followup.send("The coordination poll is open and ready for votes.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    result = await close_coordination_poll_workflow(interaction.guild, guild_id, interaction.channel)
    if not result.ok:
        await interaction.followup.send("There is no coordination poll open to close.", ephemeral=True)
        return
    await interaction.followup.send("The coordination poll has been closed; results were posted.", ephemeral=True)


async def _tracking_member_access_ok(
    interaction: discord.Interaction,
    guild_id: str,
    *,
    guest_message: str,
    tracking_message: str,
) -> bool:
    if get_access_tier(str(interaction.user.id)) == "guest":
        await interaction.response.send_message(guest_message, ephemeral=True)
        return False
    if get_guild_settings(guild_id)["member_opt_in_enabled"] and not has_tracking(
        str(interaction.user.id), guild_id
    ):
        await interaction.response.send_message(tracking_message, ephemeral=True)
        return False
    return True


async def _queue_access_ok(interaction: discord.Interaction, guild_id: str) -> bool:
    return await _tracking_member_access_ok(
        interaction,
        guild_id,
        guest_message="Guests can't use the private queue. Join with the enrollment panel first.",
        tracking_message="Join the book club with the enrollment panel before managing your private queue.",
    )


@discord.app_commands.describe(
    action=OPTION_DESCRIPTIONS["queue"]["action"],
    title=OPTION_DESCRIPTIONS["queue"]["title"],
    author=OPTION_DESCRIPTIONS["queue"]["author"],
)
@discord.app_commands.choices(action=[
    discord.app_commands.Choice(name="List privately", value="list"),
    discord.app_commands.Choice(name="Add a book", value="add"),
    discord.app_commands.Choice(name="Remove a book", value="remove"),
])
async def slash_queue(
    interaction: discord.Interaction,
    action: discord.app_commands.Choice[str],
    title: str | None = None,
    author: str | None = None,
):
    """Ephemeral private-queue list/add/remove."""
    if not interaction.guild:
        await interaction.response.send_message("Use this command in your book-club server.", ephemeral=True)
        return
    guild_id = str(interaction.guild.id)
    if not await _queue_access_ok(interaction, guild_id):
        return
    user_id = str(interaction.user.id)
    display_name = str(interaction.user.display_name)
    if action.value == "list":
        embed, view = private_queue_payload(interaction.user.id, guild_id)
        kwargs = {"embed": embed, "ephemeral": True}
        if view is not None:
            kwargs["view"] = view
        await interaction.response.send_message(**kwargs)
        return
    if not title or not title.strip():
        await interaction.response.send_message(
            "Add and remove need a book title.", ephemeral=True,
        )
        return
    args = {"title": title.strip(), "author": (author or "").strip() or None}
    await interaction.response.defer(ephemeral=True)
    if action.value == "add":
        ok, status = await run_book_action(action_queue_add, user_id, display_name, guild_id, args, source="manual")
        if status == "full":
            await interaction.followup.send(
                f"Your private queue is full (max 25). Remove something before adding **{args['title']}**.",
                ephemeral=True,
            )
            return
        if status == "already":
            await interaction.followup.send(
                f"**{args['title']}** is already on your private queue.", ephemeral=True,
            )
            return
        if not ok:
            await interaction.followup.send("I couldn't add that book.", ephemeral=True)
            return
        await interaction.followup.send(
            f"Added **{args['title']}** to your private queue.", ephemeral=True,
        )
        return
    ok, status = await run_book_action(action_queue_remove, user_id, display_name, guild_id, args)
    if status == "not_found":
        await interaction.followup.send(
            f"**{args['title']}** wasn't on your private queue.", ephemeral=True,
        )
        return
    if not ok:
        await interaction.followup.send("I couldn't remove that book.", ephemeral=True)
        return
    await interaction.followup.send(
        f"Removed **{args['title']}** from your private queue.", ephemeral=True,
    )


@discord.app_commands.describe(
    action=OPTION_DESCRIPTIONS["current"]["action"],
    title=OPTION_DESCRIPTIONS["current"]["title"],
    author=OPTION_DESCRIPTIONS["current"]["author"],
    date=OPTION_DESCRIPTIONS["current"]["date"],
)
@discord.app_commands.choices(action=[
    discord.app_commands.Choice(name="Show current book", value="show"),
    discord.app_commands.Choice(name="Start club book", value="start"),
    discord.app_commands.Choice(name="Set target date", value="target"),
    discord.app_commands.Choice(name="Complete club book", value="complete"),
])
async def slash_current(
    interaction: discord.Interaction,
    action: discord.app_commands.Choice[str],
    title: str | None = None,
    author: str | None = None,
    date: str | None = None,
):
    """Public current-read embed, plus admin start/target/complete."""
    if not interaction.guild:
        await interaction.response.send_message("Use this command in your book-club server.", ephemeral=True)
        return
    guild_id = str(interaction.guild.id)
    if action.value == "show":
        await interaction.response.send_message(
            embed=build_current_read_embed(get_current_read(guild_id))
        )
        return
    if not await _require_slash_admin(interaction):
        return
    if action.value == "start":
        if not title or not title.strip():
            await interaction.response.send_message(
                "Starting a club book needs a title.", ephemeral=True,
            )
            return
        args = {
            "title": title.strip(),
            "author": (author or "").strip() or None,
            "target_date": (date or "").strip() or None,
        }
        await interaction.response.defer(ephemeral=True)
        ok, status, session = await run_book_action(start_club_read,
            guild_id, str(interaction.user.id), args
        )
        if status == "invalid_date":
            await interaction.followup.send(
                "Use a target date like `2026-10-15`, or omit it.", ephemeral=True,
            )
            return
        if status == "already_active" and session:
            current_title = session.get("title") or "the current club book"
            await interaction.followup.send(
                f"The club is already reading **{current_title}**. Complete it first, then start the new book.",
                ephemeral=True,
            )
            return
        if not ok:
            await interaction.followup.send("I couldn't start that club book.", ephemeral=True)
            return
        await send_current_read(interaction.channel, guild_id)
        await interaction.followup.send("The current club book is posted.", ephemeral=True)
        return
    if action.value == "target":
        ok, status, _session = set_club_target(
            guild_id, str(interaction.user.id), date
        )
        if status == "no_session":
            await interaction.response.send_message(
                "Start a current club book before setting a target date.", ephemeral=True,
            )
            return
        if status == "invalid_date":
            await interaction.response.send_message(
                "Use a target date like `2026-10-15`, or `clear` to remove one.",
                ephemeral=True,
            )
            return
        if not ok:
            await interaction.response.send_message("I couldn't update the target date.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await send_current_read(interaction.channel, guild_id)
        await interaction.followup.send("The target date is updated.", ephemeral=True)
        return
    ok, status, _session = complete_club_read(guild_id, str(interaction.user.id))
    if status == "no_session":
        await interaction.response.send_message("There is no current club book to complete.", ephemeral=True)
        return
    if not ok:
        await interaction.response.send_message("I couldn't complete the club book.", ephemeral=True)
        return
    await interaction.response.send_message("The club's current book is complete.", ephemeral=True)


@discord.app_commands.describe(
    action="Browse a scrapbook, list sessions, add a moment, or remove your own entry",
    content="Text to save (1–1000 characters); shared with club readers",
    kind="The type of moment to save",
    session_id="Session number from Sessions; defaults to the current or latest club read",
    title="Book title to select its latest club reading session",
    entry_id="Entry number to remove (your own entries only)",
)
@discord.app_commands.choices(
    action=[discord.app_commands.Choice(name=name, value=value) for name, value in [
        ("Browse", "browse"), ("Sessions", "sessions"), ("Add", "add"), ("Remove", "remove"),
    ]],
    kind=[discord.app_commands.Choice(name=value.replace("_", " ").title(), value=value)
          for value in ("quote", "highlight", "reaction", "rating_note", "discussion")],
)
async def slash_scrapbook(
    interaction: discord.Interaction,
    action: discord.app_commands.Choice[str] | None = None,
    content: str | None = None,
    kind: discord.app_commands.Choice[str] | None = None,
    session_id: int | None = None,
    title: str | None = None,
    entry_id: int | None = None,
):
    if not interaction.guild:
        await interaction.response.send_message("Use this command in your book-club server.", ephemeral=True)
        return
    guild_id = str(interaction.guild.id)
    if not await scrapbook_access_ok(interaction, guild_id):
        return
    operation = action.value if action else "browse"
    if operation in ("browse", "sessions"):
        await send_scrapbook(interaction, guild_id, session_id=session_id, title=title, sessions=operation == "sessions")
        return
    if operation == "add":
        ok, status, entry = add_scrapbook_entry(
            guild_id, str(interaction.user.id), str(interaction.user.display_name),
            {"content": content, "kind": kind.value if kind else "highlight", "session_id": session_id, "title": title},
        )
        message = (
            f"Saved entry #{entry['entry_id']} in session #{entry['session_id']}. Club readers can see it in /scrapbook."
            if ok else ADD_ERRORS.get(status, "I couldn't save that moment.")
        )
    elif operation == "remove":
        removed = remove_scrapbook_entry(guild_id, str(interaction.user.id), entry_id)
        message = "Your scrapbook entry was removed." if removed else "No entry of yours matches that number in this server."
    else:
        message = "Choose Browse, Sessions, Add, or Remove."
    await interaction.response.send_message(message, ephemeral=True)


async def slash_history(interaction: discord.Interaction):
    """Public club archive: finished books, recent-first, with ratings."""
    if not interaction.guild:
        await interaction.response.send_message("Use this command in your book-club server.", ephemeral=True)
        return
    history = get_club_history(str(interaction.guild.id))
    items = []
    for row in history:
        detail = f"{row['finishes']} finished"
        if row.get("finishers"):
            detail += f" • {row['finishers']}"
        if row.get("avg_score") is not None:
            detail += f" • ⭐ {row['avg_score']}/10"
        items.append({"title": row["title"], "author": row.get("author"), "detail": detail})
    pages = make_list_embeds("📚 Club history", items, kind="milestone", empty="The club hasn't finished any books yet.")
    if len(pages) == 1:
        await interaction.response.send_message(embed=pages[0])
        return
    view = PagedEmbedView(pages, invoker_id=interaction.user.id)
    await interaction.response.send_message(embed=pages[0], view=view)
    view.message = await interaction.original_response()


def _decode_csv_bytes(data: bytes) -> str | None:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


@discord.app_commands.describe(file=OPTION_DESCRIPTIONS["import"]["file"])
async def slash_import(interaction: discord.Interaction, file: discord.Attachment):
    """Ephemeral Goodreads finished-history import with a confirm button."""
    if not interaction.guild:
        await interaction.response.send_message("Use this command in your book-club server.", ephemeral=True)
        return
    guild_id = str(interaction.guild.id)
    if not await _tracking_member_access_ok(
        interaction,
        guild_id,
        guest_message=IMPORT_GUEST_MESSAGE,
        tracking_message=IMPORT_TRACKING_MESSAGE,
    ):
        return
    filename = (file.filename or "").lower()
    if not filename.endswith(".csv"):
        await interaction.response.send_message(
            "Attach a `.csv` file exported from Goodreads.",
            ephemeral=True,
        )
        return
    if file.size and file.size > MAX_FILE_BYTES:
        await interaction.response.send_message(
            "That file is too large. Export from Goodreads and attach a CSV under 5 MB.",
            ephemeral=True,
        )
        return
    await interaction.response.defer(ephemeral=True)
    payload = await file.read()
    if len(payload) > MAX_FILE_BYTES:
        await interaction.followup.send(
            "That file is too large. Export from Goodreads and attach a CSV under 5 MB.",
            ephemeral=True,
        )
        return
    text = _decode_csv_bytes(payload)
    if text is None:
        await interaction.followup.send("This isn't a Goodreads export.", ephemeral=True)
        return
    user_id = str(interaction.user.id)
    preview = parse_goodreads_csv(
        text,
        existing_shelf_titles=existing_shelf_titles(user_id, guild_id),
    )
    if not preview.ok:
        await interaction.followup.send("This isn't a Goodreads export.", ephemeral=True)
        return
    embed = build_import_preview_embed(preview)
    if not preview.to_add:
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    view = GoodreadsImportView(
        guild_id=guild_id,
        owner_id=interaction.user.id,
        display_name=str(interaction.user.display_name),
        rows=preview.to_add,
    )
    message = await interaction.followup.send(embed=embed, view=view, ephemeral=True)
    view.message = message


async def slash_export(interaction: discord.Interaction):
    """Ephemeral Goodreads-shaped CSV of the caller's shelf in this guild."""
    if not interaction.guild:
        await interaction.response.send_message("Use this command in your book-club server.", ephemeral=True)
        return
    guild_id = str(interaction.guild.id)
    if not await _tracking_member_access_ok(
        interaction,
        guild_id,
        guest_message=EXPORT_GUEST_MESSAGE,
        tracking_message=EXPORT_TRACKING_MESSAGE,
    ):
        return
    result = build_shelf_export(str(interaction.user.id), guild_id)
    embed = build_export_embed(result)
    if not result.csv_text:
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    payload = BytesIO(result.csv_text.encode("utf-8-sig"))
    await interaction.response.send_message(
        embed=embed,
        file=discord.File(payload, filename=EXPORT_FILENAME),
        ephemeral=True,
    )


def register(tree) -> None:
    """Attach the slash commands to an app-command tree (called from bot.py)."""
    tree.command(name="help", description=COMMAND_DESCRIPTIONS["help"])(slash_help)
    tree.command(name="status", description=COMMAND_DESCRIPTIONS["status"])(slash_status)
    tree.command(name="admin-dashboard", description=COMMAND_DESCRIPTIONS["admin-dashboard"])(slash_admin_dashboard)
    tree.command(name="nominations", description=COMMAND_DESCRIPTIONS["nominations"])(slash_nominations)
    tree.command(name="nominate", description=COMMAND_DESCRIPTIONS["nominate"])(slash_nominate)
    tree.command(name="poll", description=COMMAND_DESCRIPTIONS["poll"])(slash_poll)
    tree.command(name="coordination", description=COMMAND_DESCRIPTIONS["coordination"])(slash_coordination)
    tree.command(name="queue", description=COMMAND_DESCRIPTIONS["queue"])(slash_queue)
    tree.command(name="current", description=COMMAND_DESCRIPTIONS["current"])(slash_current)
    tree.command(name="history", description=COMMAND_DESCRIPTIONS["history"])(slash_history)
    tree.command(name="scrapbook", description=COMMAND_DESCRIPTIONS["scrapbook"])(slash_scrapbook)
    tree.command(name="import", description=COMMAND_DESCRIPTIONS["import"])(slash_import)
    tree.command(name="export", description=COMMAND_DESCRIPTIONS["export"])(slash_export)
