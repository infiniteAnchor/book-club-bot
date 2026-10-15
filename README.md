# Book Club Bot

A self-hostable Discord bot for a small reading group. Natural-language-first, with `/commands` to support or offer alternatives. Claude handles intent parsing and response generation; Python handles all system logic.

## Features

### Personal tracking

- **Book tracking** — add, start, update progress, finish, rate, DNF with mock eulogies, plus contextual progress/finish/quit/done buttons
- **Google Books lookup** — automatic author, page count, and genre
- **Private reading queue** — ephemeral `/queue` with start/remove; public wishlist stays visible to the club
- **Goodreads import and export** — `/import` a library export CSV to add finished books to your personal shelf (recommendations and taste; `/history` and the leaderboard stay club-logged). `/export` downloads your shelf as a Goodreads-shaped CSV for StoryGraph, Hardcover, or Goodreads. [Spec](docs/goodreads-history-import.md)
- **Personal reminders** — “Remind me tomorrow to read Dune” DMs when due; repeating daily/weekly/monthly rows reschedule
- **Goals and streaks** — yearly book goals, reading streaks that count each logged day, and a club leaderboard

### Club

- **Current club book** — one shared reading session per server with start date, optional target date, member progress, `/current`, and a poll-winner handoff
- **Nominations & polls** — nominate via chat, `/nominate`, or the Nominate button; vote with Discord buttons; tiebreaker on manual and auto-close; `/nominations` / `/poll` admin commands. Nominating requires the Club tier
- **Coordination polls** — scheduling and pacing discussions, including `/coordination`
- **Club history** — browsable archive of finished books, recent-first, with finisher names and average ratings, via chat or `/history`
- **Club scrapbook** — save quotes, highlights, reactions, rating notes, and discussion memories per club read; browse privately with `/scrapbook` or chat. Shared with club readers; may contain spoilers. [Guide](docs/scrapbook.md)
- **Spoiler threads** — per-book threads for finished books
- **Participation roles** — Chat, Tracking, Updates, and Club, plus optional DM Nudges; enrollment panel and targeted role mentions.

### Engagement

- **Weekly recaps** — automated summaries every Saturday
- **Recommendations** — mood-based suggestions from reading history
- **Taste profiles** — quarterly LLM-generated reading profiles
- **Achievements** — 9 types, announced on milestones
- **Year in review** — annual stats and narrative
- **Inactivity nudges** — DM reminders for stale progress

### Running the bot

- **Admin dashboard** — private `/admin-dashboard` overview with live club details, per-server settings, and admin poll/tone controls
- **Operational health** — ephemeral `/status` shows bot uptime, application version (`config.APP_VERSION` as single source of truth), SQLite database size, Discord latency, and LLM provider/model (no secrets exposed) in the warm embed style; handles missing metrics gracefully
- **Graceful error handling** — unexpected failures in chat, slash commands, buttons/modals, and scheduled jobs are logged centrally (`errors.py`) with full tracebacks and context (action, guild, user, command, job), while users only ever see a short static friendly message — never secrets, SQL, paths, or tracebacks. Slash-command fallbacks use `interaction.response` before a defer and `followup` after; one failed scheduled job never stops the scheduler or other guilds
- **Help hub** — natural-language help plus an ephemeral `/help` guide, grouped by category and role

## Privacy and third-party services

Self-hosting keeps the database on your machine, but some data is sent to
outside services. Tell your members before you enable the bot.

- **LLM provider** (Anthropic, OpenAI or DeepSeek, whichever you configure):
  every message that mentions the bot, plus that member's recent books,
  progress, reading goal, streak, taste profile and the current club read.
  Recommendations, recaps, leaderboards, year-in-review and scheduled posts
  also send club activity, including member display names, ratings and review
  notes.
- **Google Books**: book titles and authors, to look up metadata.
- **Discord** stores the messages, as with any bot.

Each provider's own data-retention and training policies apply. Discord user
IDs are not sent to the LLM, and performance logs contain no message content
(see [Performance logs](#performance-logs)).

## Self-hosting

The bot runs as one Python process with a SQLite file in `data/`. It is built for
one small club per Discord server and can join several servers.

### 1. Create the Discord application

1. In the [Discord Developer Portal](https://discord.com/developers/applications),
   create an application and add a bot. Copy its token.
2. Under **Bot → Privileged Gateway Intents**, turn on **Message Content** (the
   bot reads natural-language requests) and **Server Members** (participation
   roles and member names). The bot will not start without them.
3. Invite it with the `bot` and `applications.commands` scopes. It needs these
   permissions: View Channels, Send Messages, Send Messages in Threads, Create
   Public Threads, Manage Threads, Embed Links, Attach Files, Read Message
   History, Add Reactions and **Manage Roles**. Keep the bot's role above the
   roles it creates (`Book Club · Chat`, `· Tracking`, `· Updates`, `· Club`,
   `· DM Nudges` and `Book Club Admin`), or role changes fail.
4. Create a `#book-bot` channel, or change the channel names below. Mention the
   bot to talk to it: `@Book Club Bot started Dune`. Both the bot's user mention
   and its Discord-managed bot role mention work; other role mentions do not.

### 2. Configure

```bash
cp .env.example .env
```

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `DISCORD_TOKEN` | yes | | Bot token |
| `LLM_PROVIDER` | | `anthropic` | `anthropic`, `openai` or `deepseek` |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `DEEPSEEK_API_KEY` | the one for your provider | | LLM key |
| `LLM_MODEL` | | per provider (`claude-sonnet-4-6`, `gpt-4o`, `deepseek-chat`) | Model override |
| `GOOGLE_BOOKS_API_KEY` | recommended | | Author, page count and genre lookup. Works unauthenticated at a low quota |
| `ENV` | | `prod` | `dev` uses `DISCORD_TOKEN_DEV` when set (else `DISCORD_TOKEN`) and prints parsed intents |
| `DISCORD_TOKEN_DEV` | | | Separate token for a dev bot |
| `DEBUG_INTENT` | | on in dev, off in prod | Print parsed intents to stdout |
| `ADMIN_DISCORD_ID` | | | Your Discord user ID. Receives DMs for prompt-injection attempts and LLM auth, billing, quota and rate-limit errors |
| `LLM_ALERT_COOLDOWN_MINUTES` | | `720` | Suppress duplicate LLM alerts for this long |
| `OPT_IN_WELCOME_CHANNEL` | | `book-bot` | Where the enrollment panel is posted |
| `RELEASE_ANNOUNCEMENTS_ENABLED` | | `false` | Post curated `CHANGELOG.md` releases (see [Release announcements](#release-announcements)) |
| `RELEASE_ANNOUNCEMENTS_CHANNEL` | | `book-bot` | Channel for the `What's new` board |
| `APP_VERSION` | | newest `CHANGELOG.md` date | Version shown in `/status` |

Per-server club settings (schedules, channels, tone, polls) live in the database
and are changed from `/admin-dashboard`. The constants in `config.py` are only
the defaults for a newly joined server.

Typical LLM cost for one small club is a few dollars a month. Chat is
rate-limited per member before any LLM call.

### 3. Run

With Docker:

```bash
docker compose up -d --build
docker compose logs -f
```

`./data` is mounted into the container, so the database survives rebuilds.

Or in a local virtualenv (Python 3.11+):

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python bot.py
```

Slash commands sync on startup. The first time the bot sees a server it creates
its roles and settings row and makes the server owner a Book Club Admin.

### Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q --ignore=tests/live_intents.py
python tests/simulate.py
```

`tests/live_intents.py` calls the real LLM and costs API credits; it is opt-in
and never runs in CI. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Architecture

```
Discord message → bot.py → llm.py (LLM: intent + response) → actions.py → database.py (SQLite)
```

- Python is system of truth — all DB reads/writes, scheduling, validation
- Claude is passive — only runs when Python calls it
- Single call mode: one Claude call returns `{action, args, response}`
- Chat replies: Claude writes the response. Slash commands, dashboard, enrollment, and vote toasts are Python-owned embeds
- Natural-language-first: all book-club actions work via chat; `/commands` support or offer alternatives to the same actions
- Errors funnel through `errors.py`: logs keep full tracebacks + context; Discord only ever receives static friendly fallbacks (`errors.FRIENDLY_ERROR_MESSAGE`)
- LLM calls from chat and scheduled jobs run on a dedicated pool of up to four workers. Provider requests use a 30-second SDK timeout with automatic retries disabled; this is a network timeout, not an end-to-end deadline. Messages from the same reader in a guild are processed in order.
- Google Books metadata is fetched off the event loop before book actions run; SQLite mutations remain synchronous. Interactions defer before slow lookups, and existing book metadata is reused.

### Performance logs

The `bookclub.performance` logger emits content-free metrics at INFO level:

- `request`: total accepted chat processing time (including waiting for an earlier message from the same reader), validated action, LLM call count, cumulative provider time, and failures.
- `llm`: provider time, success/failure, and input/output token counts when returned by the provider. A random request ID connects chat calls to their request summary; scheduled and standalone calls use `request_id=none`.

These metrics do not contain message text, book titles, model responses, or member IDs. Provider time includes SDK/client overhead; request time includes lookup and Discord delivery time. A provider failure can produce a friendly fallback, so inspect `llm_failures` alongside the overall `failed` flag. Multiple LLM calls remain where a response needs fresh action results; the logs make those costs visible. Live-provider speed and intent accuracy still require opt-in API testing.

## Stack

Python · discord.py · SQLite · APScheduler · Multi-provider LLM (Anthropic / OpenAI / DeepSeek)

Runtime pins live in `requirements.txt`. Optional local lint/type tools are configured in `pyproject.toml`:

```bash
pip install ruff mypy
ruff check .
mypy .
```

## Release announcements

Announcements are opt-in so maintenance work and ordinary commits never post to
Discord. For a member-facing release, add both `announce: true` and a curated
`### Announcement` section to its `CHANGELOG.md` entry:

```md
## [2026-08-31] (Easier scheduling)
announce: true

### Announcement
- Vote on the next reading schedule with `/coordination`.
- Browse the improved help hub with `/help`.
```

Only the bullets in that section are eligible to be posted; omit the marker for
internal fixes, refactors, dependency updates, and other maintenance releases.
Set `RELEASE_ANNOUNCEMENTS_ENABLED=true` when ready to send them. Each server
admin can then enable or disable release notices from `/admin-dashboard`.
On startup (and when the bot joins a server) the bot syncs one persistent
`📚 What's new` embed in `RELEASE_ANNOUNCEMENTS_CHANNEL` (default: `book-bot`).
Newest curated releases are listed first; later releases edit that message
instead of posting another. The bot never @s or tags anyone on that board, or
on weekly recaps, check-ins, or year-in-review. A per-guild lock and stored
message IDs keep overlapping ready events from stacking duplicates; a missing
or moved message is posted once and the new IDs are saved after Discord accepts
it.

Shipped history is in [CHANGELOG.md](CHANGELOG.md). Doc map:
[docs/README.md](docs/README.md).

## Next work

BCB-001 through BCB-010 are resolved. Shared session, `/history`, `/import`,
`/export`, and `/scrapbook` are in. Remaining product work is how those surfaces connect:
an end-of-session wrap, a session-aware archive, then optional pacing. Details
and patch order: [docs/known-gaps.md](docs/known-gaps.md).

## Future optional

In suggested order; patch details in [docs/known-gaps.md](docs/known-gaps.md).

- **End-of-session wrap** — collect remaining ratings, point at the scrapbook, and leave room for the next nomination window when a club book completes
- **Session-aware archive** — show canonical session dates, suggester, and poll winner on `/history`
- **Current-read pacing** — weekly page or section checkpoints after members use the target date
- **Richer book presentation** — cover images, descriptions, ISBN search. Google Books already supplies author, page count, and genre
- **StoryGraph / Hardcover / TBR import** — later; Goodreads finished-history is the v1
- **Scrapbook capture follow-ons** — message bookmarking, reaction ranking, automatic recap posts
- **Reading sprints** — only if members already organize live co-reading

Automated discussion prompts, a built-in event calendar, and multiple clubs per
server stay out of scope. See [known-gaps.md](docs/known-gaps.md) **Not next**.
