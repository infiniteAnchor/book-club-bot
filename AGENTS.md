# AGENTS.md — Book Club Bot

Self-hostable Discord bot for a small reading group. Natural-language-first, with `/commands` to support or offer alternatives. Python + discord.py + SQLite + Claude API.

## Read Before Editing

1. `README.md` — features, setup, architecture overview
2. `docs/known-gaps.md` — current next work (BCB-001–BCB-010 resolved in `BUGS.md`)
3. `docs/README.md` — doc map. Historical plans in `docs/` are marked shipped; do not re-implement them

## Architecture

```
message → bot.py → llm.py (Claude: intent parsing) → actions.py (Python: DB + logic) → response
```

- Python is system of truth — all DB reads/writes, scheduling, validation
- Claude is passive — only runs when Python calls `get_intent()` in `llm.py`
- Chat replies: Claude writes the response (never raw DB output). Slash commands, dashboard, enrollment, and vote toasts are Python-owned embeds
- `SINGLE_CALL_MODE = True` — one Claude call returns action + response together
- Guild context threaded through all actions (`guild_id`) — shelf writes key `reading_progress` by `(user_id, book_id, guild_id)`

## Code Layout

| File | Purpose |
|------|---------|
| `bot.py` | Discord client, event handlers, startup wiring (`register(commands)`) |
| `commands.py` | Slash commands: `/help`, `/admin-dashboard`, `/nominations`, `/nominate`, `/poll`, `/coordination`, `/queue`, `/current`, `/history`, `/scrapbook`, `/import`, `/export`, `/status` |
| `handlers.py` | `RequestContext` + all natural-language action handlers + dispatch table |
| `poll_workflows.py` | Shared nomination/poll/coordination open-close path used by chat, slash commands, and the admin dashboard |
| `poll_threads.py` | Temporary poll-thread create/archive/delete + persisted thread IDs |
| `participation_roles.py` | Managed Discord roles, bootstrap, reconcile, `set_participation` |
| `release_announcements.py` | Persistent changelog embed: edit in place, never pings |
| `release_notes.py` | CHANGELOG `announce: true` / `### Announcement` parser |
| `help_content.py` | Static `/help` and NL help copy |
| `status.py` | `/status` embed: uptime, version, DB, latency, LLM, Python runtime |
| `telemetry.py` | Content-free request and LLM timing logs (`bookclub.performance`) |
| `embeds.py` | Shared embed palette and builders |
| `views.py` | Public re-export facade for Discord Views/Modals |
| `views_polls.py` | Book-poll and coordination voting Views plus Nominate button/modal |
| `views_enrollment.py` | Enrollment panel View + join-message tracking |
| `views_books.py` | Reading-shelf and book-action Views/Modals |
| `views_queue.py` | Private queue reveal, next-book picker, save-destination Views |
| `views_import.py` | Goodreads CSV import preview + confirm View, plus `/export` embed |
| `views_dashboard.py` | Admin dashboard embed, settings modals, `AdminDashboardView` |
| `jobs.py` | APScheduler setup, job registries, all scheduled jobs + notify helpers |
| `sending.py` | Message-sending helpers + copy formatters |
| `errors.py` | Centralized error handling — context logging, redaction (plus a redacting log formatter for every handler), static Discord fallbacks (message path, interaction path, view/modal mixin) |
| `rate_limit.py` | Per-user chat token buckets checked in `on_message` before any LLM call (smaller budget before enrollment) |
| `helpers.py` | Cross-layer helpers (reply channel, admin role, name lookups) |
| `actions.py` | All business logic — 30+ handlers |
| `actions_queue.py` | Private reading queue + wishlist remove/save/next-book helpers |
| `actions_nominations.py` | Nomination/poll/coordination logic |
| `actions_session.py` | Canonical shared reading session (current club book) |
| `actions_scrapbook.py` | Session scrapbook entries, owner removal, and club aggregates |
| `views_scrapbook.py` | Spoiler-safe ephemeral scrapbook pages and reveal controls |
| `actions_social.py` | Public re-export facade for social/participation helpers |
| `actions_participation.py` | Participation tiers, feature prefs, release-announcement settings |
| `actions_nudges.py` | Inactivity nudges + reminders |
| `actions_engagement.py` | Digests, achievements, goals, streaks, tone, admin summary |
| `actions_import.py` | Goodreads finished-history CSV parse/apply, plus personal shelf CSV export |
| `llm.py` | `get_intent()` — single function, intent schema in `VALID_ACTIONS` |
| `database.py` | SQLite schema + ordered migrations + `connect()` |
| `guild_settings.py` | Per-guild club settings: `ensure_guild_settings`, get/update, new-guild defaults from `config.py` |
| `config.py` | Process flags + **new-guild** club defaults (runtime club knobs live in `guild_settings`) |

## Conventions

- **Natural-language-first** — all book-club actions work via chat; `/commands` support or offer alternatives
- **LLM intent schema** in `llm.py:VALID_ACTIONS` — add new actions there first, then `handlers.INTENT_ACTIONS` with `admin_only` / `guest_blocked` flags
- **Config flags** in `config.py` — process-level plus **new-guild** club defaults; runtime club knobs live in `guild_settings` (`ensure_guild_settings` is idempotent; existing rows are not rewritten when defaults change)
- **Scheduled jobs** via APScheduler — per-guild feature toggles live in `guild_settings`; `config.py` values are new-guild defaults only
- **Admin gating** uses the managed `Book Club Admin` role, plus server owner / Administrator / Manage Server fallbacks in `has_admin_role`. Not the DB `access_tier` column
- `access_tier` is only used for guest blocking (`GUEST_BLOCKED_ACTIONS`)
- `progress_pct` computed automatically from `page_count` in `action_update_progress`

### Changelog and Discord announcements

`CHANGELOG.md` and the Discord `What's new` board are different layers. Git already records every code touch; do not add a changelog heading for every file change.

**Never set `announce: true` on your own.** Agents are not allowed to opt a release onto the Discord board automatically. The user must say to announce, or answer yes when asked. While waiting, write the changelog entry *without* the marker.

When you add a changelog heading for a member-visible change and the user has not already said whether to announce, **ask**: "Want this on the What's new board?" Propose yes or no using the table. Do not ask for test-only, CI, refactors, agent-docs, dependency pins, or other work members will not notice.

| Recommend | When |
|-----------|------|
| **Yes** (still ask unless they already said announce) | Club members will see or use it: new/changed chat actions, slash commands, enrollment, polls, current book, recaps, help copy they read in Discord |
| **No** | Internal fixes, tests, CI, refactors, docs for agents, schema/ops notes, pins, anything that does not change what members see |

A changelog entry (no announce) is still worth writing for a shippable unit you would want to remember in six months: member-visible behavior, ops-relevant fixes, schema/deploy notes. Omit `announce: true` and `### Announcement` unless Discord should show it.

If announcing, add **both** `announce: true` and a short `### Announcement` with member-facing bullets only. Parser and board behavior: `release_notes.py` / `release_announcements.py`; human summary in `README.md` (Release announcements).


## Design Guardrails

- No SQLAlchemy / DI framework / CQRS — keep it dependency-light
- Don't churn the `actions*.py` split or the single-call LLM architecture
- No big-bang refactors; land one module at a time with `py_compile` + existing tests as guardrail
- Split oversized modules when you touch them. A `src/` package move can land with a tooling pass; no hexagonal/engine rewrite

## Build & Test

```bash
python bot.py                          # Run
python -m pytest tests/ -q --ignore=tests/live_intents.py   # CI (Google Books stubbed; fast SQLite)
python tests/simulate.py               # scenario assertions (pure DB, no API)
python -m compileall -q -x '/(\.venv|venv|data|\.git)/' .  # CI compile check
python tests/live_intents.py           # OPT-IN: intent parsing vs real API (~40 calls) — never in CI
python tests/capture_intent_fixtures.py  # OPT-IN: refresh tests/fixtures/intent_cases.json, then commit it
python tests/dry_run_intents.py         # OPT-IN: dummy messages → live parser → planned actions in data/dry_run_intents.json (no Discord)
python tests/seed.py                   # Seed test data
```

CI runs pytest + `simulate.py` + `compileall` on Python 3.11 (same as Docker). `live_intents.py` is ignored.

Debug flags:
- `CONVERSATIONAL_RESPONSES=False` in config.py — raw JSON output
- `DEBUG_INTENT=True` — parsed intents in stdout

## Current State

- **Branch:** `master` (all phases complete)
- **CI:** push to `master` → `CI` (GitHub-hosted tests + gitleaks)
- **Database:** `data/bookclub.db` — persists across rebuilds
- **API cost:** ~$1–3/month (separate Anthropic account)
- **Roadmap:** see `docs/known-gaps.md` (end-of-session wrap, then session-aware archive)
