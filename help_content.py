"""Static, Python-owned copy for the bot's help experience."""
from __future__ import annotations

from typing import Iterable

from embeds import make_embed


HELP_TOPICS = ("books", "club", "polls", "stats", "membership", "admin")

_TOPIC_CARDS = {
    "books": {
        "title": "📚 Help: books",
        "description": "Track what you want to read, what you're reading, and what you've finished.",
        "fields": [
            ("Start and update", "`I started Dune`\n`I'm on page 120 of Dune`"),
            ("Finish and rate", "`I finished Dune`\n`Rate Dune 9 out of 10`"),
            (
                "Lists and ideas",
                "`Add Piranesi to my private queue`\n`Show my queue` or `/queue`\n"
                "`Remove Piranesi from my queue`\n"
                "`Add Dune to my wishlist`\n`Recommend something like Dune`",
            ),
            (
                "Import and export",
                "On Goodreads desktop: My Books → Import and export → Export Library. "
                "Then `/import` and attach that CSV. This fuels recommendations and your "
                "personal shelf; `/history` and the leaderboard stay club-logged books.\n"
                "`Export my shelf` or `/export` downloads your shelf here as a CSV you can import into "
                "Goodreads, StoryGraph, or Hardcover. The file includes your private queue as to-read.",
            ),
        ],
    },
    "club": {
        "title": "👥 Help: club activity",
        "description": "Keep up with what the group is reading and talk safely about finished books.",
        "fields": [
            (
                "See the club",
                "`What are we reading?` or `/current`\n"
                "`What's everyone reading?`\n`Give us a weekly recap`",
            ),
            ("Spoiler discussion", "`Open a spoiler thread for Dune`\n(available after you finish the book)"),
            ("Club scrapbook", "`/scrapbook` — quotes, highlights, reactions, rating notes, and discussion memories. "
             "Shared with club readers; opened privately because of spoilers.\n"
             "`Save a highlight to the scrapbook: The desert imagery stayed with me`\n"
             "`Show the scrapbook for Dune` · `Remove my scrapbook entry 12`\n"
             "Use `/scrapbook action:Add` to submit without posting the text in chat; "
             "`action:Sessions` lists past reads."),
            ("Reminders", "`Remind me tomorrow to read Dune`"),
        ],
    },
    "polls": {
        "title": "🗳️ Help: nominations and polls",
        "description": "Nominate the club's next read, vote with buttons, and coordinate the schedule.",
        "fields": [
            (
                "Nominate",
                "`/nominate` or the Nominate button (Club tier)\n"
                "`Nominate Piranesi by Susanna Clarke because…`\n"
                "`Show the current nominations`",
            ),
            ("Vote and results", "Use the buttons on an open poll.\n`Show poll results`"),
            ("Coordination", "`Show coordination poll results`"),
        ],
    },
    "stats": {
        "title": "🏆 Help: goals and stats",
        "description": "See your consistency, reading goal, and friendly club standings.",
        "fields": [
            ("Your progress", "`I want to read 12 books this year`\n`How am I doing on my goal?`"),
            ("Streaks", "`What's my reading streak?`\n`Show the streak leaderboard`"),
            ("Club stats", "`Show the leaderboard`\n`How was our reading year?`\n`What have we read?`"),
        ],
    },
    "membership": {
        "title": "⚙️ Help: membership and nudges",
        "description": (
            "Four tiers — Tracking, Updates, Club, and Chat only — plus optional DM nudges."
        ),
        "fields": [
            (
                "The four tiers",
                "**Tracking** — log books, rates, and streaks\n"
                "**Updates** — + weekly recaps (not DM nudges)\n"
                "**Club** — + nominations, polls, and voting\n"
                "**Chat only** — server chat without those member features",
            ),
            (
                "Join a tier",
                "`Join the book club`\n`Get club updates too`\n"
                "`I want everything: nominations and polls`\n`Chat only, please`",
            ),
            (
                "Nudges & settings",
                "`Turn off reading nudges` (independent of Updates)\n"
                "`What are my book club settings?`",
            ),
        ],
    },
    "admin": {
        "title": "🛠️ Help: admin tools",
        "description": "Admin-only workflows for running the club.",
        "fields": [
            (
                "Choose the next read",
                "`/nominations open` · `/poll open`\n`/nominations list` · `/poll results`\n"
                "`/current start` · `/current complete`",
            ),
            ("Plan together", "`/coordination open question:Which Wednesday? options:4th\\n11th\\n18th`"),
            ("Club controls", "`/admin-dashboard` (settings, member roles, admin assignment, role sync)\n`Set Alex's tone to roast`"),
            ("Operational health", "`/status` — uptime, version, database, latency, and LLM provider/model"),
        ],
    },
}


def _add_fields(embed, fields: Iterable[tuple[str, str]]) -> None:
    for name, value in fields:
        embed.add_field(name=name, value=value, inline=False)


def build_help_embed(
    topic: str | None = None,
    *,
    is_admin: bool = False,
    is_enrolled: bool = True,
    is_dm: bool = False,
    welcome_channel: str = "#book-bot",
):
    """Build the member-facing help hub or a topic-specific card.

    welcome_channel should already be a display label (#name or <#id>).
    """
    normalized = (topic or "").strip().lower()
    if normalized == "admin" and not is_admin:
        normalized = ""
    if normalized in _TOPIC_CARDS:
        card = _TOPIC_CARDS[normalized]
        embed = make_embed(card["title"], card["description"], kind="info")
        _add_fields(embed, card["fields"])
    else:
        embed = make_embed("📚 Book Club Bot help", "Talk to me naturally, or use `/help` to open this guide anytime.", kind="info")
        _add_fields(embed, [
            ("📚 Books", "Track books, update progress, rate, browse your shelf, and get recommendations.\nTry: `I started Dune`"),
            ("👥 Club", "See the current club book, group activity, recaps, spoiler threads, and reminders.\nTry: `What are we reading?`"),
            ("🗳️ Polls", "Nominate books with `/nominate` or chat, vote with buttons, and see results.\nTry: `Show the current nominations`"),
            ("🏆 Stats", "Set goals, check streaks, and view leaderboards.\nTry: `Show the leaderboard`"),
            ("⚙️ Membership", "Join Tracking, Updates, Club, or Chat only, and manage reading-nudge DMs.\nTry: `What are my book club settings?`"),
        ])
        if is_admin:
            _add_fields(embed, [("🛠️ Admin", "Run nominations, polls, coordination, dashboard tools, and `/status` health checks.\nTry: `Help with admin tools`")])

    # welcome_channel is a pre-formatted label (#name or <#id>), not a raw setting.
    if is_dm:
        _add_fields(embed, [("Join from your server", f"Use the enrollment buttons in {welcome_channel} to join the book club, then come back here anytime.")])
    elif not is_enrolled:
        _add_fields(embed, [("Join the book club", f"Use the enrollment buttons in {welcome_channel} to pick your tier and enable member features.")])
    return embed


def build_unknown_help_embed(response: str):
    """Keep an unrecognized request useful without replacing language guardrails."""
    embed = make_embed("🤔 I didn't quite catch that", response, kind="info")
    _add_fields(embed, [
        ("Try one of these", "`I started Dune`\n`What am I reading?`"),
        ("Need a guide?", "Say `help` or use `/help` for every category and example."),
    ])
    return embed
