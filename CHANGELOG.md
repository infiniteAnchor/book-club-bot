# Changelog

## [2026-10-14] (Multiple requests per chat message, experimental)

### Changed
- One chat message can carry up to 3 requests (e.g. "finished Dune, rate it 9/10, starting Hyperion"). They run in the order asked, each with its own join-the-club and guest checks; a denial notice is sent once per kind.
- Each request after the first sees the shelf as earlier requests left it.
- If part of a message can't be understood, the bot handles the rest, says it missed a part, and skips the ✅.
- If one request fails, the ones after it don't run.
- Intent replies may use up to 2048 output tokens so three-request replies aren't cut off.

### Added
- `tests/dry_run_intents.py`: opt-in harness that runs sample messages through the live parser and writes planned actions to `data/dry_run_intents.json`. No Discord or DB.

## [2026-09-29] (Self-hosting docs and public-safe CI)

### Added
- README self-hosting guide: Discord intents and permissions, every env var, `LLM_PROVIDER`, Docker and local venv.
- `SECURITY.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1), issue forms and a PR template.

### Changed
- CI runs on GitHub-hosted runners with a read-only token and adds a gitleaks scan.

## [2026-09-29] (Chat rate limits and log redaction)

### Changed
- Chat messages from other bots and webhooks are ignored.
- Each member can send up to 10 chat messages a minute to the bot (3 before joining the club). Past that, the bot says so once and skips the rest without calling the LLM.
- `ENV` now defaults to `prod`. With `ENV=dev`, the bot uses `DISCORD_TOKEN_DEV` if set and falls back to `DISCORD_TOKEN`.
- Prompt-injection admin alerts include at most the first 300 characters of the flagged message.

### Security
- Every log line, tracebacks included, is scrubbed of configured secrets (Discord tokens, LLM keys, Google Books key) and Discord/Google-shaped tokens. discord.py logs now go through the same redacting handler.

## [2026-09-29] (Open-source hygiene and MIT license)

### Changed
- Licensed under MIT (`LICENSE`); `pyproject.toml` declares it.
- `docker-compose.yml` mounts `./data` instead of an absolute host path.
- Test deps now install from `requirements-dev.txt` (CI uses it). Removed the stub `uv.lock`.
- Local agent settings (`.claude/settings.local.json`, `.claude/launch.json`, `.codex`) and `.env.*` are no longer tracked; the Docker build context skips `.venv/`, `.claude/`, `.github/` and `.orch/`.

## [2026-09-28] (Personal shelf CSV export)
announce: true

### Announcement
- Take your shelf with you: use `/export` or ask `Export my shelf` to download it as a CSV you can import into Goodreads, StoryGraph, or Hardcover.
- Only you see the file. It includes your private queue as to-read, so check it before uploading anywhere.
- Books you didn't finish are marked read with a `dnf` tag, since Goodreads has no DNF shelf.

### Added
- `/export` downloads your personal shelf in this server as a Goodreads-shaped CSV you can import into Goodreads, StoryGraph, or Hardcover.
- The file is private, includes finished, currently-reading, wishlist, and private-queue titles (as to-read), and marks DNF as read with a `dnf` bookshelf tag.
- Asking in chat ("export my shelf") points you to `/export`, so shelf and queue titles never appear in a public channel.

### Fixed
- `/import` skips rows tagged `dnf`, so re-importing an export no longer turns DNFs into finished books.
- Titles or authors starting with `=`, `+`, `-`, or `@` are exported as text so spreadsheet apps don't run them as formulas.

### Tests
- Offline coverage for shelf mapping, live-vs-import collapse, guild isolation, roundtrip through the Goodreads parser, slash registration, and guest/Tracking/DM access.
- Regression coverage for formula escaping, DNF re-import, and the `export_shelf` chat intent.

## [2026-09-27] (Typing indicator while the bot thinks)

### Changed
- The bot shows "Book Club Bot is typing…" while it works out what a chat message means, and again during the follow-up call that phrases replies with fresh data (shelf and group queries, reading goals, nomination-linked ratings, nomination lists, and poll results).

## [2026-09-27] (Faster, cheaper chat replies)

### Changed
- The chat intent call caches its ~2.5k-token system prompt with Anthropic prompt caching. Tone overrides go after the cache breakpoint so glaze/roast users still hit the cache. OpenAI/DeepSeek and the other LLM calls are unchanged.
- Per-message database work (guild settings, poll-thread lookup, server upsert, chat context) runs off the event loop. The server upsert happens once per guild name per process, and the chat context shares one connection instead of opening four or more.

### Operations
- `init_db` switches the database file to WAL mode, and connections set an explicit 5 s busy timeout, so scheduled jobs and chat writes wait instead of failing with `database is locked`.
- Migration 14 adds the `reading_progress_user_guild` index. Applies on normal startup.
- LLM performance logs now include `cache_read_tokens` and `cache_write_tokens`.

## [2026-09-26] (Reading streak fix)
announce: true

### Announcement
- Reading streaks now count every day you log progress, even on the same book. Check yours with `What's my streak?`.
- Your streak holds until the end of the day after your last update, so logging tomorrow keeps it going.
- Streaks recorded before this fix may restart from a shorter count.

### Fixed
- Reading streaks now count every day you log progress, including repeat updates on the same book; previously they could not grow past one day.
- A streak stays current through the day after your last update instead of dropping to 1.
- Streak achievements and the streak leaderboard use the same corrected count.

### Operations
- Migration 13 adds the `reading_activity` table (one row per member, server, and UTC day) and backfills it from existing shelf timestamps. Applies on normal startup. Older history kept only one date per book, so existing streaks start from what survives. Wishlist adds and Goodreads imports do not count as reading days.

### Tests
- Offline coverage for daily progress on one book, the yesterday grace day, longest-run calculation, backfill filtering, and guild-scoped leaderboard ranking.

## [2026-09-25] (Club scrapbook)
announce: true

### Announcement
- Save quotes, highlights, and reactions from the club read: ask `Save this to the scrapbook: …` or use `/scrapbook`.
- Browse the current or past scrapbooks privately, so spoilers stay hidden until you open them.

### Added
- Save quotes, highlights, reactions, rating notes, and discussion memories for each club reading session through chat or `/scrapbook`.
- Browse current and completed scrapbooks privately, with saved moments, session dates, and existing club ratings; remove your own entries by number.
- Keep scrapbook replies spoiler-safe, scope entries to their server and session, and recheck membership on browsing controls.

### Operations
- Migration 12 adds the scrapbook entries table and session index without rewriting existing data. Applies on normal startup.
- Club rating and finish totals span reads of the book and exclude Goodreads imports; saved moments remain separate for each read.

### Tests
- Offline coverage for migration persistence, session and guild boundaries, owner removal, import exclusion, slash registration, spoiler-safe replies, interaction gates, and the empty session-list message.
- Live Discord delivery and real-provider intent classification remain unrun.

## [2026-09-25] (Responsiveness and performance diagnostics)

### Fixed
- Compare reading streaks against the UTC date used by stored activity timestamps, fixing timezone-dependent streak resets and CI failures.
- Move LLM calls and Google Books lookups off Discord's event loop; keep database mutations synchronous and each reader's chat requests ordered.
- Defer book-action interactions before lookups and safely deliver provider-error alerts from worker threads.

### Changed
- Bound LLM execution to four workers and configure a 30-second SDK network timeout without automatic retries.
- Log content-free chat duration, LLM call counts, provider duration, token usage, and failures.

### Tests
- Add offline responsiveness, concurrent request isolation, alert-thread, and provider-timeout checks.
- Add queue and wishlist-removal intent examples to offline parser tests and the opt-in live suite; real-provider captures remain pending.

## [2026-09-15] (Goodreads finished-history import)

### Added
- `/import` accepts a Goodreads library CSV, previews finished-book counts, and writes personal shelf history after an Import confirm.
- Imported rows use `source = goodreads`, skip Google Books and achievements, and stay off `/history`, recaps, year-in-review, and the leaderboard.

### Changed
- Starting or DNF-ing an imported title logs a new Discord row instead of rewriting the Goodreads finish, so a club pick you already read on Goodreads can still be tracked.
- Imported ratings keep `source = goodreads` and the CSV date, so a later Discord re-read does not put that score on recaps, year-in-review, or club averages.

### Tests
- Fixture CSV parse/apply, ignore counts, idempotent re-apply, no Google Books, no achievements, club vs personal query split, `/import` wiring, live start/DNF after import, imported ratings staying off club surfaces after a re-read, and Import confirm access/double-click.

## [2026-09-03] (club history archive)
announce: true

### Announcement
- Browse what the club has finished with `/history`, or ask `What have we read?`.
- Each finished book shows who finished it and the club's average rating.

### Added
- Natural-language `club_history` plus `/history` show finished club books, recent-first, with finisher names and average ratings.
- Club archive reads existing completion and rating data; no migration. DNF excluded, capped at 50 books, paginated at 10 per page.

### Tests
- Empty guild, recent-first ordering, multi-finisher names, rating averages, guild isolation, DNF exclusion, limit cap, and `/history` registration.

## [2026-09-02] (changelog board)
announce: true

### Announcement
- Release notes now live in one `What's new` message that the bot edits when something ships, instead of a new post each time.
- Weekly recaps, check-ins, and year-in-review no longer tag anyone.

### Changed
- Curated changelog entries sync into a single persistent embed per server (newest first). Later releases edit that message; a deleted or moved board is posted once and the new message IDs are stored after Discord accepts it.
- Weekly recaps, check-ins, combined weekly posts, and year-in-review send with no role mention and `AllowedMentions.none()`.

### Tests
- Board post/edit/skip, channel move, forbidden fetch, overlapping sync, opt-out, and changelog packing coverage.

## [2026-09-02] (current club book)
announce: true

### Announcement
- Ask `What are we reading?` or use `/current` to see the club's current book, dates, and member progress.
- A poll winner now becomes the current club book when one is not already in progress.

### Added
- Canonical shared reading session per guild (migration 9): one active club book, start date, optional target date, and status.
- Natural-language `current_read` / `manage_club_read` plus `/current` show, start, target, and complete.
- Current-read embed with member progress; poll close starts a session when the club is idle.
- Admin dashboard overview names the club book.

### Fixed
- Polls with no votes no longer promote a sole nomination into the current club book.
- Oversized relative target dates return normal validation errors instead of overflowing datetime arithmetic.
- A rejected attempt to start another club book no longer persists an unused book record.

### Tests
- Session start/replace/complete, guild isolation, target parsing, poll-winner handoff, dashboard copy, `/current` command wiring, and help examples.

## [2026-09-02] (offline Google Books in tests)
### Changed
- pytest and `simulate.py` stub Google Books HTTP so creating books never waits on the network.
- Test runs set `BOOKCLUB_TEST_FAST_SQLITE` so SQLite skips durable fsyncs; production connects are unchanged.

### Tests
- `lookup_google_books` parser coverage from fake payloads (match, page-count bounds, errors, query/key).

## [2026-09-02] (poll auto-close overlap)
### Fixed
- Auto-close jobs use a stable `auto-close:{kind}:{guild_id}` id with `replace_existing`, so overlapping reconnects cannot register two date jobs (BCB-009).
- `on_ready` runs under one process lock; nomination and coordination auto-close go through the shared workflows and no-op when nothing is open.

### Tests
- Overlapping auto-close schedules keep one job; nomination/coordination auto-close no-op when closed; `on_ready` lock wiring.

## [2026-08-31] (guild-scoped shelves)
### Fixed
- Start, progress, and finish look up `reading_progress` by user, book, and guild, so a second server no longer updates the first server’s row (BCB-007).
- “Add this to my shelf” uses the same 25-cap and guild filter as the wishlist helper (BCB-008).

### Tests
- Cross-guild start/progress/finish isolation, `add_book` per-guild insert, wishlist cap on `add_book`, and the full-shelf chat notice.

## [2026-08-31] (enrollment copy, nominate, version)
announce: true

### Announcement
- Enrollment now names Tracking, Updates, Club, and Chat only. Weekly recaps are Updates; DM nudges stay a separate toggle.
- Nominate from `/nominate` or the Nominate button on the nominations thread (Club tier).

### Fixed
- Enrollment panel no longer says Updates includes inactivity nudges. Chat only is explained. Membership help names the four tiers.
- Guest denials no longer say “core access tier”.
- `APP_VERSION` follows the newest CHANGELOG date heading (the old `0.1.0` placeholder is treated as unset).
- Nomination inputs share bounded title/author/reason lengths, and long titles are shortened only on voting buttons to stay within Discord's component limit.
- Release announcements use an in-process delivery guard for overlapping `on_ready` calls and persist completion only after a successful send, so cancellation or restart can retry.

### Added
- `/nominate` and a persistent Nominate button (modal) on the nomination thread. Club members can submit or replace a pick without using chat.

### Tests
- Enrollment copy, guest-denial source guard, membership help tiers, `/nominate` registration and input bounds, nomination apply/replace, long-title vote buttons, cancellation-safe announcement retry, and APP_VERSION matching the latest changelog heading.

## [2026-08-30] (known-gaps first five)
### Fixed
- Tied book-poll auto-close and restart recovery now go through `close_book_poll_workflow`, so a tie opens a runoff instead of stopping at “ended in a tie” (BCB-002).
- Tiebreaker nomination windows are inserted as `closed`, so `get_active_window` no longer treats a runoff as an open nomination round (BCB-003).
- Personal reminders now DM when due. A one-minute sweep restores on startup, deactivates one-shots, and advances daily/weekly/monthly rows (BCB-004).
- `Show my queue` no longer posts titles in-channel. Public ack plus an ephemeral reveal; `/queue` lists/adds/removes privately; the reveal has start and remove controls (BCB-005).
- Chat nominations require the Club tier when member opt-in is on (BCB-006).

### Changed
- CI runs every `tests/test_*.py` file, `python tests/simulate.py`, and `compileall` on Python 3.11 (same as Docker). `live_intents.py` stays opt-in.

### Tests
- Poll auto-close launches the shared workflow; tiebreaker leaves nominations closed; reminder due/advance/DM coverage; queue public-ack and Club-gate nomination tests.

## [2026-08-30] (known-gaps backlog)
### Changed
- Documented the 2026-08-30 review: open defects BCB-002–BCB-009 in `BUGS.md`, recommended next patches and skip list in `docs/known-gaps.md`, README/AGENTS pointers, and the CI allow-list drift (what Actions runs vs tests that exist).

## [2026-08-30] (enrollment panel restart)
### Fixed
- Startup enrollment-panel sync no longer posts a duplicate when the stored message ID is missing or 404s. It reuses an existing welcome-channel panel (enrollment buttons or the Join Book Club embed) and only sends a new one when none is found.

### Tests
- `tests/test_guild_settings.py` covers stale/missing stored IDs, button-id reuse, join-mention exclusion, empty-channel create, and unreadable history.

## [2026-08-29] (personal reading queues)
### Added
- Private per-member `reading_queue` (migration 8), separate from the public wishlist shelf.
- Natural-language `queue_add` / `queue_remove` / `queue_list` and `remove_from_wishlist`, with guest-blocked writes.
- Combined next-book picker after finish/DNF (private queue + public wishlist), revealed ephemerally via owner-only controls.
- Save-to-queue or wishlist buttons on structured recommendations and nomination surfaces.
- `actions_queue.py` and `views_queue.py` for CRUD, destination saves, and Discord views.

### Changed
- Recommendations request structured JSON (`intro` + `books`); plain-text fallback keeps the embed without save buttons.
- Help books topic covers private queue phrasing.

### Tests
- `tests/test_reading_queue.py`, `tests/test_recommendation_parse.py`; migration and intent-registry coverage for version 8 and new actions; simulate section 16.

## [2026-08-28] (channel settings as Discord IDs)
### Changed
- Guild channel settings now store Discord snowflake IDs when a matching text channel exists. `config.py` defaults remain channel names for new guild rows only.
- Startup and guild-join upgrade name-valued channel settings to IDs in place; unresolved names keep working via dual-read.
- Jobs, poll workflows, listen/reply routing, enrollment, release announcements, and chat handlers resolve channels by ID or name through a shared helper.
- Admin dashboard channel modals still accept typed names, resolve them to IDs on save, and show `#channel` mentions for ID-backed settings.

### Tests
- Added `tests/test_channel_settings.py` for coerce/resolve/upgrade/label behavior and dashboard mention rendering.

## [2026-08-28] (L1 layout hygiene)
### Changed
- Split oversized `views.py` into focused modules (`views_polls`, `views_enrollment`, `views_books`, `views_dashboard`) behind a thin public facade.
- Split oversized `actions_social.py` into `actions_participation`, `actions_nudges`, and `actions_engagement` behind a thin public facade.
- Documented optional L1–L4 layout uplift levels in `docs/layout-uplift-levels.md`.

### Tests
- Updated embed/mention and poll-workflow caller scans for the new view modules.
- Enrollment dashboard patches now target `views_enrollment` where the symbols live.

## [2026-08-26] (quieter notifications)
### Fixed
- Year-in-review startup recovery now runs only during the 24 hours after its January 1 09:00 schedule, so ordinary restarts later in the year remain silent. Recovery is guild-specific and preserves per-guild idempotency.

### Removed
- Broad `@everyone` pings from enrollment panels, scheduled weekly posts, year-in-review posts, nomination and poll close results, and unresolved-tiebreaker notices. Workflow-opening anchors retain their existing broadcast ping until managed participation roles replace it.

### Tests
- Notification guards now ensure the workflow-opening anchor is the only remaining `@everyone` send, close announcements are silent, and enrollment-panel refreshes clear legacy mention content.
- Year-in-review recovery coverage includes August startup, a legitimately missed January run, an existing event, and repeated ready/reconnect events.

## [2026-08-26] (participation roles review fixes)
### Fixed
- Participation changes now update Discord roles before committing the SQLite authorization mirror, and missing role-management permissions can no longer grant database-only access.
- Admin authorization resolves the registered managed-role ID, so adopted roles work and same-name collisions cannot grant unintended access.
- Membership changes sent by DM resolve the member in their live guild and use the same Discord-plus-SQLite synchronization path as server messages.
- Recreated participation roles restore member assignments from SQLite before normal Discord-to-database reconciliation, preserving Club and DM Nudge preferences after role deletion.

### Tests
- Added regressions for failed Discord writes, missing Manage Roles, managed-admin ID authorization, DM member resolution, and deleted-role recovery.

## [2026-08-26] (participation roles follow-up)
### Fixed
- Direct-message guild resolution now joins the user table correctly, restoring DM intent handling for members enrolled in exactly one server.
- The bot now bootstraps managed roles immediately after joining a server instead of waiting for its next reconnect.
- Choosing Tracking or Updates now downgrades higher tiers as expected; tier changes preserve the separate DM Nudge setting whenever Tracking remains enabled.

### Tests
- Added regression coverage for direct-message guild lookup and tier downgrades.

## [2026-08-26] (participation roles phase 5)
### Added
- Enrollment panel buttons for Tracking, Updates, Club, Chat only, and an independent nudges toggle, all going through `set_participation`.
- Admin dashboard Roles page: per-member tier/nudge controls, grant/remove Book Club Admin with last-admin protection, Sync roles, and a confirmation-gated adopt-existing-role action.

### Changed
- Chat `manage_membership` writes Discord roles when the author is a guild member.
- Enrollment copy and `/help` admin text describe the four-tier model.

### Tests
- Role adoption and last-admin protection in `test_participation_roles.py`; enrollment custom IDs, `set_participation` wiring, and dashboard Roles controls in `test_enrollment_dashboard.py`.

## [2026-08-26] (participation roles phases 3-4)
### Added
- `sending.role_broadcast_payload`: managed-role mention + restricted `AllowedMentions` for broadcasts (ping, visible-but-silent, or silent when the role is missing); `send_embeds`/`send_summary` gained an additive `allowed_mentions` pass-through.

### Changed
- Scheduled updates carry a visible-but-silent Updates mention; nomination, poll, coordination, and tiebreaker openings ping Club; close results and the enrollment panel remain fully silent. No path emits `@everyone` or `@here`.
- Voting gates require Club; book-action and enrollment gates require Tracking via new `has_tracking`/`has_club` helpers. The `manage_membership` intent now takes `{operation: join | opt_out | show | enable_nudges | disable_nudges, tier: chat | tracking | updates | club}`; help, prompt, and gate copy use tier language instead of "Core Book Club".

### Tests
- Broadcast guard rejects `@everyone`/`@here` literals and requires explicit `allowed_mentions=` on content-bearing sends; role payload, authorization helper, handler mapping, and offline intent-fixture coverage added.
- Scheduled-job isolation tests mock role broadcast resolution, so clean CI runners do not require a local role database.

## [2026-08-26] (participation roles phase 2)
### Added
- `participation_roles.py` service: managed-role registry lookup by stored ID, safe creation (never adopts, renames, or deletes existing roles; suffixed names on collisions; deleted roles replaced), `set_participation` with cumulative Discord/SQLite mirroring, `on_member_update` mirroring that ignores unrelated changes and is idempotent for bot-originated updates, one-time bootstrap with completion-marker semantics, startup reconciliation, and rejoin restoration.
- Server-owner emergency admin fallback in `has_admin_role`; owner receives `Book Club Admin` during bootstrap.

### Changed
- `bot.py` starts the client only under `__main__`, so importing the module no longer launches a live session.

### Tests
- `tests/test_participation_roles.py`: registry safety, cascade assignment, listener idempotency, bootstrap marker semantics, reconcile repairs, and owner fallback.

## [2026-08-25] (participation roles phase 1)
### Added
- Migration 7: `managed_roles` registry and `role_sync_state` marker tables, plus a feature-row rewrite that maps Core members to Tracking only and drops orphaned DM Nudge rows.

### Changed
- Member preferences now use the four-key participation model (`tracking`, `updates`, `club`, `dm_nudges`) with cumulative tier cascades; DM Nudges requires Tracking but never changes Updates or Club.
- Enrollment gates, membership intents, enrollment buttons, help, and DM guild resolution follow the Tracking key; `get_single_core_guild` is now `get_single_participation_guild`.

### Tests
- Migration test replays migration 7 over legacy rows; simulate.py section 15 covers cascade rules in the four-key model.

## [2026-08-25] (participation-role execution)
### Added
- Phased execution plan for participation roles: DB migration and four-key preferences, `participation_roles.py` service, targeted-notification helper, authorization cutover, and enrollment/dashboard updates, with resolved policy decisions and per-phase verification gates.

## [2026-08-25] (participation-role design)
### Added
- Implementation plan for managed Chat, Tracking, Updates, Club, independent DM Nudge, and manual Admin Discord roles. The plan defines targeted notification audiences to replace `@everyone`, startup migration and role synchronization, owner bootstrap, channel/thread behavior, implementation seams, and test coverage.

### Changed
- README roadmap now links the participation-roles and targeted-notifications design plan.

## [2026-08-25] (shared poll workflows)
### Changed
- Nomination windows, book polls, and coordination polls now open and close through `poll_workflows.py`. Natural-language chat, `/nominations` `/poll` `/coordination`, and the admin dashboard share the same job scheduling, poll-thread posting, and tiebreaker follow-up.

### Tests
- `tests/test_poll_workflows.py` covers already-open / not-open short circuits, thread and job wiring, tiebreaker follow-up, coordination option validation, and a source guard that callers no longer invoke the action open/close functions directly.

## [2026-08-25] (intent registry)
### Changed
- Natural-language actions are declared once in `handlers.INTENT_ACTIONS` (`handler`, `admin_only`, `guest_blocked`). Dispatch, guest blocking, and opt-in exemption sets are derived from that table.
- `bot.py` no longer keeps a local guest-block list. Enrollment skip uses `OPT_IN_EXEMPT_ACTIONS` (admin-only actions plus `help` / `manage_membership`). Admin authorization is still checked inside each admin handler.
- Dropped the unused guest-block name `replace_my_nomination` (not a valid intent; replacement goes through `nominate`).

### Tests
- `tests/test_intent_registry.py` locks the registry to `llm.VALID_ACTIONS` (except `silent`), freezes the admin/guest sets, and source-guards that every `admin_only` handler still calls `has_admin_role`.
- Migration coverage now derives its expected schema versions from `database.MIGRATIONS`, preventing the CI assertion from going stale when migrations are added.

## [2026-08-25] (dev tooling config)
### Added
- `pyproject.toml` with ruff (core correctness rules, 120-char lines) and mypy (`ignore_missing_imports`, discord.py `method-assign` silenced). Pyright settings live in the same file.

### Changed
- Runtime dependency pins in `requirements.txt` were already in place; the README roadmap now marks that work shipped.
- Tool caches (`.ruff_cache/`, `.mypy_cache/`) are gitignored.

### Fixed
- Six f-strings in `handlers.py` reused the outer quote inside `{...}` (Python 3.12-only). They now parse on the Docker image's 3.11 runtime.

## [2026-08-24] (temporary poll threads)
### Added
- Nomination windows, book polls, tiebreakers, and coordination polls now use dedicated public Discord threads with their opening cards, voting controls, and results kept together.
- Poll-thread records persist parent/message/thread IDs and cleanup state so active views, auto-close, locking, archiving, and deletion recover across restarts.
- Per-guild poll-thread retention is configurable from `/admin-dashboard` → Server settings. New and existing guilds default to 7 days; `0` keeps closed threads indefinitely.

## [2026-08-24] (per-guild admin settings)
### Added
- Each Discord server now has a `guild_settings` row created deterministically by `ensure_guild_settings(guild_id)` during guild initialization (`on_ready`, `on_guild_join`, and the first settings read). The insert is idempotent (`ON CONFLICT DO NOTHING`).
- `/admin-dashboard` includes a Server settings page: toggle features, edit channels, schedule, and durations. Saving schedule or feature flags reschedules only that guild's recap/check-in/nudge jobs.
- Club runtime (opt-in, mentions, listen/reply channels, recap/nudge timing, poll destinations) reads the guild row, not live `config.py` constants.

### Changed
- `config.py` club flags remain as **defaults for new guilds only**. Editing those constants later does not rewrite existing rows; change existing guilds with an admin update or an explicit data migration.
- Release-announcement destination is per guild. The global `RELEASE_ANNOUNCEMENTS_ENABLED` kill switch is unchanged.
- Enrollment panels now reconcile immediately when opt-in is enabled or its welcome channel changes; disabling opt-in retires the stored panel. A stored message id with no channel id is reused in the current welcome channel instead of posting a second `@everyone` panel.
- Listen-channel-only settings reject an empty listen channel, preventing a guild from silently ignoring every message.

### Fixed
- Enrollment-panel reconciliation preserves the stored panel on permission or transient Discord API failures instead of posting a duplicate `@everyone` panel.
- Removing the bot from a guild cancels its nomination, book-poll, and coordination auto-close jobs as well as its periodic jobs.

### Tests
- `tests/test_guild_settings.py` covers idempotent ensure, config-default isolation for existing rows, validation, dashboard embed copy, job reschedule and cleanup, enrollment panel create/edit/move/retire (including a missing stored channel id and failed fetch), and migration version 4.

## [2026-08-24] (command descriptions)
### Changed
- Slash-command picker copy is longer and more specific: `/help`, `/status`, `/admin-dashboard`, `/nominations`, `/poll`, and `/coordination` now say what each command is for instead of sharing near-identical "open, close, or view" blurbs.
- Option descriptions explain who can list/view versus open/close, and action choice labels name the workflow (`Open nominations`, `Open book poll`, `Open scheduling poll`).

### Tests
- `tests/test_commands.py` guards Discord's 100-character description cap and that `register()` / `describe()` use the documented copy.

## [2026-08-24] (embeds everywhere)
### Changed
- Remaining member-facing Discord output now uses the warm embed palette: recommendations, empty leaderboard/streak/goal states, chat validation and denials, membership replies, tone overrides, nomination/poll redirects, enrollment panels, inactivity-nudge DMs, English-only unknowns, and the reading-shelf book picker.
- Ephemeral interaction toasts, the static `FRIENDLY_ERROR_MESSAGE` fallback, operator DMs, and debug JSON stay plain text. Mentions (`@everyone`, member pings) still go in `content=` so Discord actually pings.

### Tests
- `tests/test_embeds.py` covers `send_notice` / `make_embed` palette and Discord length trim, recommend and enrollment/nudge/shelf-picker embeds, and a source guard that `handlers.py` and `bot.py` do not send bare member-facing strings.

## [2026-08-23] (release announcements)
### Added
- Opt-in member-facing release announcements: only a changelog entry with both `announce: true` and a curated `### Announcement` section is eligible to post. The bot sends only the entry matching `APP_VERSION`, once per enabled guild, to the configured release channel.
- Global `RELEASE_ANNOUNCEMENTS_ENABLED` master switch (off by default), per-guild admin-dashboard controls, and a configurable `RELEASE_ANNOUNCEMENTS_CHANNEL`.
- Ordered SQLite migrations with `schema_version` tracking, including persistent guild announcement settings and successful-delivery records. Failed sends remain eligible for retry.

## [2026-08-23] (graceful error handling)
### Added
- New centralized `errors.py`: every unexpected exception now flows through one path that logs the full traceback plus operational context (surface, action, guild, user, command, job) to logs only, and answers users with a single static friendly message (`FRIENDLY_ERROR_MESSAGE`) that can never leak API keys, SQL, tracebacks, internal paths, or provider response bodies. `redact()` scrubs key tokens, key assignments, tracebacks, and paths from any dynamic text.
- Natural-language path (`bot.on_message`): the two broad handlers are consolidated into `errors.handle_message_failure`, which resolves the reply channel defensively and swallows even fallback-delivery failures. Context (parsed action, guild, user) is captured for the log even when a handler dies mid-flight.
- Slash commands: `command_tree.on_error` in bot.py routes every app-command failure through `errors.handle_interaction_failure`, which unwraps `CommandInvokeError`, uses `interaction.response` when nothing was sent yet and `followup.send` after a defer (both ephemeral), treats `CheckFailure` as an expected authorization rejection (logged quietly, no scary fallback), and never raises itself.
- Buttons/modals: shared `errors.SafeUIMixin.on_error` applied to all interactive Views/Modals in views.py so component callbacks get the same log-and-fallback treatment.
- Scheduled jobs (`jobs.py`): new `_launch`/`_watch_future` pair runs coroutines on the bot loop from scheduler threads and retrieves their Futures' exceptions — previously coroutine failures vanished silently because `run_coroutine_threadsafe` futures were never awaited. Per-guild try/except isolation added to weekly recap, check-in prompts, combined weekly post, taste profiles, nudges, year-in-review, and auto-close jobs, so one failed guild or job never stops the scheduler or other guilds.

### Preserved
- LLM failure behavior unchanged: provider errors still return `llm.fallback_intent()` with the friendly "book brain" copy, actionable failures (auth/billing/quota/rate-limit) still DM the admin via `schedule_llm_failure_alert`, still suppressed by `LLM_ALERT_COOLDOWN_MINUTES`. Single-call architecture, action ownership, and admin/guest authorization untouched; expected validation messages still come from their own handlers.

### Tests
- `tests/test_error_handling.py` (33 offline tests): static-fallback + full-context logging for handler exceptions; LLM/API failure fallbacks incl. alert wiring, non-actionable silence, handler-crash tolerance, cooldown suppression; slash response-vs-followup behavior incl. post-defer and delivery-failure safety; scheduled-job isolation (failed/cancelled futures, dead event loop, per-guild continuation); redaction of keys/paths/tracebacks; source guards that bot.py/jobs.py stay on the central path. Added to CI pytest lists; `errors.py` added to compile checks.

## [2026-08-23] (/status operational health)
### Added
- Ephemeral `/status` slash command showing bot uptime, application version, SQLite database size, Discord/WebSocket latency, and current LLM provider/model (no secrets), built with the warm embed style and Discord-safe field lengths. Handles missing database files and unavailable metrics gracefully.
- `config.APP_VERSION` (`APP_VERSION` env, default `0.1.0`) and `config.BOT_START_TIME` as the single central source of version/uptime truth; `status.py` formatting helpers (`format_uptime`, `format_db_size`, `format_latency`, `get_llm_display`, `get_db_size_str`, `get_uptime_str`, `build_status_embed`) keep secrets out of the response.
- `/status` registration via the existing `commands.register(tree)` pattern; invite scope docs now list `/status`.
- Help documentation updated: `help_content` admin card and hub now surface `/status`, README features/setup and roadmap updated.
- Focused tests: `tests/test_status.py` covers formatting, missing-database behavior, embed metadata/limits, and command registration; version is only read from `config`.

## [2026-08-22] (quit + done buttons on book action prompt)
### Added
- Picking a book from the reading-shelf dropdown now also offers a **Quit** button (danger) alongside Update progress / Finish. It verifies the book is still `reading`, marks it DNF via `action_dnf_book`, checks achievements (`dnf_survivor`), and edits the prompt to the "🪦 Book laid to rest" embed — deterministic copy, no LLM call.
- **Done** returns inline shelf action messages to the public shelf instead of deleting the shelf message.
## [2026-08-22] (fix: shelf buttons rejected their own owner)
### Fixed
- "What am I reading" shelf dropdown and its Update Progress / Finish buttons told even the owner "Only you can use this." and refused every action. `ctx.author_id` is a string (`str(message.author.id)`) but the views compared it against `interaction.user.id` (an int), so the ownership check could never pass. `ReadingListView` and `BookActionView` now normalize `owner_id` to `int()` in their constructors, fixing all call sites.

## [2026-08-21] (scheduler dedup + debug gating)
### Changed
- Collapsed the three near-identical auto-close schedulers (`schedule_auto_close`, `schedule_poll_auto_close`, `schedule_coordination_poll_auto_close`) and their `cancel_*` twins into one shared `_schedule_auto_close`/`_cancel_auto_close` implementation (`jobs.py` −74 lines). Public names and behavior unchanged.
- `DEBUG_INTENT` is now environment-controlled: on in dev, off in prod, overridable via `DEBUG_INTENT=1|0` env var (previously hardcoded `True`, printing every parsed intent in prod).

### Fixed
- Chat `close_poll` now cancels the poll auto-close job (`cancel_poll_auto_close`) instead of the nomination one. Previously the poll's auto-close job kept running after a manual close and later posted a bogus "No one voted in this poll." message.

## [2026-08-21] (refactor: layered modules)
### Changed
- Split `bot.py` (2594 → 328 lines) into layered modules: `handlers.py` (all 32 natural-language action handlers + `RequestContext` + dispatch table), `views.py` (all Views/Modals + admin dashboard embed), `jobs.py` (APScheduler + job registries + all scheduled jobs), `commands.py` (all 5 slash commands via `register(tree)`), `sending.py` (send helpers + copy formatters), `helpers.py` (cross-layer helpers). No circular imports; `client` shared via `jobs.set_client()`.
- DB access now goes through `database.connect()` — path resolved at call time, so tests can redirect the DB by patching `database.DB_PATH` (removes the per-module path-patching `simulate.py` used to do).
- `bot.py` now calls `llm.*` module-qualified, making LLM calls mockable/patchable in tests.
- Intent test suite restructured: CI-safe golden-fixture replay (`tests/test_intents_replay.py`), opt-in live runner (`tests/live_intents.py`, never in CI), fixture capture script (`tests/capture_intent_fixtures.py`), shared cases in `tests/intent_cases.py`.
- CI (both workflows) now runs `tests/test_intents_replay.py` alongside `test_english_only`.

### Fixed
- Added missing `cancel_poll_auto_close` — closing a poll via the admin dashboard or `/poll` previously raised `NameError` after the poll was already closed.

## [2026-08-21]
### Fixed
- Missing `import re` in `bot.py` caused `NameError` crash in `ProgressUpdateModal.on_submit` when parsing progress input.
- `ProgressUpdateModal` title exceeded Discord's 45-char limit (prefix + 70-char book title = up to 87 chars). Capped book title portion to 27 chars.
- `RatingModal` title had the same overflow issue (prefix + 90 chars). Capped to 38 chars.

### Added
- Reading list query now shows a dropdown to pick a book, then presents Update Progress / Finish buttons for it. Only appears on the user's own reading shelf.

## [2026-08-20]
### Added
- Contextual reader-only buttons after tracking a book: modal progress updates, one-click finishing, and modal ratings.
- Direct `/nominations`, `/poll`, and `/coordination` commands for opening, closing, and viewing club voting workflows without LLM intent parsing.
- Private `/admin-dashboard` slash command with live club detail views, poll workflow controls, confirmation steps, and member tone override controls.

## [2026-08-20]
### Added
- Role-aware help hub, available through natural language and the new ephemeral `/help` slash command.
- Admin alerts for LLM authentication, credit/billing, quota, and rate-limit failures, with a configurable cooldown to prevent duplicate notifications.
- Native member opt-in controls for Core Book Club and inactivity-nudge DMs, scoped per member and server.
- Persistent enrollment panels for existing and newly joined members in the configured welcome channel.
- Warm, semantic Discord embeds for book activity, achievements, recaps, reviews, polls, nominations, and club statistics.
- Button pagination for long shelves, leaderboards, nominations, and poll-result lists.
- Unit coverage for embed metadata, Discord length limits, and pagination state.

### Changed
- Inactivity-nudge recipients can now reply to the bot by DM without an @mention; server messages still require one.
- Inactivity nudges now include an example progress reply.
- Inactivity nudges are sent only to members who enabled both Core Book Club and DM nudges; failed DMs are not marked as sent.
- Consolidated docs: new README.md, rewrote AGENTS.md, deleted CLAUDE.md and MASTER_REFERENCE.md (3 docs, down from 5). Added Roadmap section to README.


## [2026-06-10]
### Added
- `INACTIVITY_NUDGE_SCHEDULED` config flag to gate the DM inactivity nudge (biweekly private messages to users with stale reading progress). Wrapped scheduling in `bot.py` behind this flag, matching the pattern used by `TASTE_PROFILE_SCHEDULED`, `COMBINED_WEEKLY_POST`, etc.

## [2026-06-08]
### Added
- Message reaction acknowledgments: 👀 when bot sees and begins processing a message, ✅ after successful reply. Silent/guest-blocked/error paths show eyes but skip check mark.
- English-only guardrail tests (`tests/test_english_only.py`).

### Changed
- `LISTEN_CHANNEL_ONLY` default → `False`, `LISTEN_CHANNEL_NAME` → `""` (bot listens in all channels by default).
- Year-in-review messages now truncated at 2000 chars with a note if they exceed Discord's limit.

## [2026-06-07]
### Fixed
- `misfire_grace_time=1` on all 5 APScheduler jobs caused silent skips on any delay >1s. Raised to 3600s for weekly/biweekly jobs, 86400s for yearly/quarterly.
- `action_rate_book` referenced undefined `guild_id` variable (NameError), silently broken behind broad exception handler. Added `guild_id=None` parameter.

### Added
- Multi-provider LLM support via `LLM_PROVIDER` config (`anthropic` | `openai` | `deepseek`). `_call_llm()` abstraction normalizes Anthropic/OpenAI response formats.
- Year-in-review startup recovery: `recover_missed_year_in_review()` runs on `on_ready` if Jan 1 cron was missed.
- New config vars: `LLM_PROVIDER`, `LLM_MODEL`, `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`.
- New dep: `openai` (lazy-imported; only loaded for non-Anthropic providers).
- Combined weekly post: `get_weekly_post()` + `schedule_weekly_post()` merges Saturday recap + check-in into one message (one `@everyone`, one API call, one DB query). Gated by `COMBINED_WEEKLY_POST=True`.

### Changed
- `CLAUDE.md`: removed stale claims (no Dockerfile, events unused, branch), updated remaining work.
- `AGENTS.md`: marked 5 completed gaps, fixed branch name from `post-phase-4` to `master`.
- Refactored `bot.py` `on_message`: 674-line if/elif chain → 60-line dict-based dispatch (`_ACTION_HANDLERS`). Each action handler is a standalone async function.
- Split `actions.py` (2323 lines) → `actions.py` (651, core CRUD) + `actions_nominations.py` (750, 18 functions) + `actions_social.py` (937, 19 functions).

### Removed
- `PHASE1_RELIABILITY_CHANGES.txt` — orphan note, all changes deployed.
