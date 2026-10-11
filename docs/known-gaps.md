# Known gaps (updated 2026-09-28)

The bot is shipped and operable. This file is the source of truth for **what to
build next**. Shipped history lives in [CHANGELOG.md](../CHANGELOG.md). Defect
IDs live in [BUGS.md](../BUGS.md). Skip the items in **Not next**.

Prefer the smallest patch that matches the next real need. Do not treat this as
a license for L2 packaging or L4 architecture.

## Open defects

None. BCB-001 through BCB-010 are resolved, including reading streaks
([BCB-010](../BUGS.md#bcb-010--reading-streaks-cannot-grow-past-one-day),
2026-09-26). The 2026-09-27 typing indicator, Anthropic prompt cache, and
SQLite WAL/index work are also in.

## Remaining validation

These are test/ops leftovers, not member-facing product work.

- **Queue intent provider captures remain pending.** Queue add/remove/list and
  wishlist-removal examples run through offline synthetic parser tests and the
  opt-in live/capture suite. The tracked uncaptured cases still need a
  real-provider capture; offline tests do not establish live classification
  accuracy.
- **Scrapbook provider and Discord checks remain manual.** Chat examples are in
  the opt-in live/capture suite; offline parser and interaction tests do not
  establish live intent accuracy or actual Discord delivery.

## Product opportunities

The 2026-08-31 research sequence is complete: shared reading session
(2026-09-02), club history (2026-09-03), Goodreads finished-history import
(2026-09-15), session scrapbooks (2026-09-25), and personal shelf CSV export
(2026-09-28). What remains is how those surfaces connect, plus polish that
members would notice.

Prefer this order. Each item is independent; land one at a time.

1. **End-of-session wrap.** Completing a club book (`/current complete` or
   chat) currently posts a one-line notice. When the session ends, collect
   remaining ratings, point at the scrapbook, and leave a natural opening for
   the next nomination window. Fumiko auto-opens a rating poll on finish;
   PeakBot’s 2026 club-ops note is that the gap between books is where clubs
   stall. Keep this a short wrap on the existing session, not a new event
   model.
2. **Session-aware club archive.** `/history` lists books members finished in
   the guild (Goodreads imports excluded), with finisher names and average
   ratings. Canonical `reading_sessions` rows already store start/complete
   dates, `started_by`, and `poll_id`. Surface those on the archive so “what
   we read together” includes the official pick, dates, and suggester.
3. **Current-read pacing.** The session has an optional target date. Weekly
   page or section checkpoints, and announcing a pace with the poll winner,
   only pay off after members actually use that date. Recaps already cover
   cadence.
4. **Richer book presentation.** Google Books already supplies author, page
   count, and genre. Cover images, descriptions, ISBN search, and
   autocomplete are polish and matching quality. They stay behind the wrap
   and archive work.
5. **Request-driven expansions.** StoryGraph / Hardcover / TBR *import*,
   favorite lists, scrapbook message-bookmarking and auto recap posts, reading
   sprints, and per-shelf privacy. Personal shelf CSV *export* (`/export`)
   shipped 2026-09-28. Pursue the rest only when members already do the
   corresponding thing (live co-reading, other catalogs, starring favorites,
   bookmarking quotes in chat).

Multiple clubs per server, calendar synchronization, automated discussion
prompts, and cross-server social profiles remain deliberate non-goals for this
small club.

## CI

`ci.yml` runs `pytest tests/ --ignore=tests/live_intents.py`,
`python tests/simulate.py`, and `python -m compileall` on Python 3.11. `ci.yml`
also runs gitleaks; mark a fake test secret with `# gitleaks:allow`. Keep new
`test_*.py` files under `tests/`; do not grow an allow-list. `live_intents.py`
stays opt-in.

## Not next

| Item | Verdict |
|------|---------|
| Shared reading milestones as a first slice | After the wrap and archive; recaps already cover cadence. |
| Spoiler-safe weekly prompts | Skip. `open_thread` exists; auto questions add LLM cost and progress-gating. |
| Event calendar + RSVP | Skip. Coordination polls + Discord Events already cover scheduling. |
| Scrapbook extensions | Automatic posts, message bookmarking, and reaction ranking remain later. |
| Multiple clubs / buddy-read platform | Skip unless the server grows beyond one small club. |
| `src/` package / hexagonal core | Not the constraint. |

Do not spend the next cycle on extra slash commands for every NL action, or
churning the `actions*.py` split.
