# Bugs

BCB-001 through BCB-010 are resolved. Patch order for remaining product work:
[docs/known-gaps.md](docs/known-gaps.md). BCB-001 had a production incident
(year-in-review recovery on 2026-08-25); the rest were found by code review.

# Resolved

## BCB-010 — Reading streaks cannot grow past one day

**Status:** Resolved 2026-09-26
**Found:** 2026-09-26 code review

### Symptom

A member who logs progress on the same book every day never builds a streak beyond 1. Separately, a member whose latest activity was yesterday sees a current streak of 1 even after several consecutive days. Both errors reach `/streak`, the streak leaderboard, and the `streak` line in the LLM user context.

### Cause

`actions_engagement.get_streaks` derives activity days from `SELECT DISTINCT DATE(last_activity_at) FROM reading_progress`. `last_activity_at` is a single column per `(user_id, book_id, guild_id)` row and every progress update overwrites it (`actions.action_update_progress`), so a reader has at most one date on record per book. Progress updates are not written to `event_log`, so no daily history exists anywhere.

The current-streak loop also exits early: when the newest date is yesterday it sets `current = 1` and `break`s instead of counting back from yesterday.

### Fix

Record one row per active day — e.g. a `reading_activity(user_id, guild_id, activity_date)` table with a unique key, written with `INSERT OR IGNORE` from start/progress/finish/DNF — and compute streaks from it. Start the current-streak walk at today if present, otherwise yesterday, then count back. History cannot be backfilled beyond the existing `last_activity_at` dates; say so in the changelog.

### Resolution

Migration 13 adds `reading_activity` and backfills it from existing `last_activity_at` dates (excluding wishlist rows and Goodreads imports). `actions.record_reading_activity` writes the UTC day on start, progress, finish, and DNF. `get_streaks`, `get_streak_leaderboard`, and the streak achievements share `_streaks_from_dates`, which keeps a streak current through yesterday. The leaderboard now scans only members with activity in the guild. Regression tests: `tests/test_streaks.py`.

## BCB-002 — Tied poll auto-close skips tiebreaker

**Status:** Resolved 2026-08-30
**Found:** 2026-08-30 code review

### Symptom

A book poll that expires on its own (or is closed during restart recovery after `closes_at`) posts “The poll ended in a tie,” archives the thread, and does not open a runoff. The club is stuck until an admin notices.

### Cause

Manual close (`/poll close`, chat, dashboard) goes through `poll_workflows.close_book_poll_workflow`, which calls `_open_tiebreaker` when `result["tied"]` and the poll is not already a tiebreaker.

Scheduled auto-close (`jobs._schedule_auto_close` → `close_poll` + `send_poll_close`) and `bot.restore_open_items` call `close_poll` directly. `tests/test_poll_workflows.py` covers the manual path only.

### Fix

Route auto-close and restart recovery through `close_book_poll_workflow`, or extract the post-close tiebreaker so every close path uses it. Treat an already-closed poll as a no-op so a second close does not post “No one voted.”

### Resolution

`jobs.schedule_poll_auto_close` and `bot.restore_open_items` call `close_book_poll_workflow(..., manual=False)`. An already-closed poll is a no-op via `get_active_poll`.

## BCB-003 — Tiebreaker leaves a live nomination window open

**Status:** Resolved 2026-08-30
**Found:** 2026-08-30 code review
**Depends on:** BCB-002 (only reachable today via manual close)

### Symptom

After a tiebreaker opens, `get_active_window` still returns an open nomination window. Members can nominate into the runoff (`close_poll` tallies every nomination on that window). Admins cannot open a real nomination round. On restart, `restore_open_items` can auto-close that window as nominations and archive the wrong thread.

### Cause

`open_tiebreaker_poll` inserts a new `nomination_windows` row with the table default `status='open'` and the same `closes_at` as the runoff poll.

### Fix

Insert the tiebreaker window as `closed` (or another non-`open` status) so `get_active_window` ignores it.

### Resolution

`open_tiebreaker_poll` inserts `status='closed'`. `get_active_window` returns None during a runoff.

## BCB-004 — Reminders confirm, then never fire

**Status:** Resolved 2026-08-30
**Found:** 2026-08-30 code review

### Symptom

“Remind me tomorrow to read Dune” stores a row and posts “Reminder set for …”. The reminder is never delivered. Help still teaches the phrasing.

### Cause

`action_set_reminder` INSERTs into `events`. Nothing in `jobs.py` (or elsewhere) SELECTs `type = 'reminder'`. Recaps, nudges, poll auto-close, and year-in-review all have jobs; this does not.

### Fix

Schedule a due-event sweep (restore pending rows on startup, isolate per guild like other jobs), or remove `set_reminder` from `llm.VALID_ACTIONS`, help, and the registry until a job exists.

### Resolution

`schedule_reminder_sweep` runs every minute from `on_ready` (`replace_existing=True`). Due rows are DM'd; one-shots deactivate; daily/weekly/monthly rows advance past now.

## BCB-005 — Private queue titles can leak in public chat

**Status:** Resolved 2026-08-30
**Found:** 2026-08-30 code review

### Symptom

`Show my queue` posts Claude’s `response` in a public embed, then a “Show privately” button. The system prompt never says not to list titles, so the private TBR often appears in-channel. There is no `/queue`. The ephemeral reveal is list-only (no remove or start).

### Cause

`_handle_queue_list` sends `intent["response"]` to the channel, then `RevealPrivateListView`. Privacy is a button, not the default.

### Fix

Public ack only (“use the button to see your private queue”); titles only on the ephemeral follow-up. Instruct the LLM not to enumerate queue titles in `response`. Add `/queue` and remove/start on the ephemeral view.

### Resolution

`_handle_queue_list` posts `QUEUE_LIST_PUBLIC_ACK`. Titles appear only on the ephemeral reveal / `/queue`. The reveal has start and remove selects. The system prompt forbids listing titles in `response`.

## BCB-006 — Tracking members can nominate via chat

**Status:** Resolved 2026-08-30
**Found:** 2026-08-30 code review

### Symptom

Enrollment copy and vote buttons say nominations/voting are Club. A Tracking-only member can still nominate or replace a nomination in chat. They cannot vote.

### Cause

NL opt-in only requires Tracking (`bot.py`). `_handle_nominate` never calls `has_club`. `nominate` is `guest_blocked` only. The participation-roles design says to use `has_club` for this.

### Fix

Deny `nominate` (and replace) unless `has_club` or member opt-in is off. Lock it in `test_intent_registry.py` / authorization tests.

### Resolution

`_handle_nominate` denies Tracking-only members when opt-in is on. Help polls copy names the Club tier.

## BCB-007 — Shelf writes omit `guild_id`

**Status:** Resolved 2026-08-31
**Found:** 2026-08-30 code review
**Likely impact:** Low in a one-guild club

### Symptom

`action_add_book`, `action_start_book`, `action_update_progress`, `action_add_to_wishlist`, and `action_finish_book` look up `reading_progress` by `user_id` + `book_id` only. `action_dnf_book` and `action_wishlist_add` filter by `guild_id`. The same Discord user in two servers can update the other guild’s row (or no-op because the other guild already has the book). Progress buttons use the unfiltered helpers.

### Fix

Always key `reading_progress` by `(user_id, book_id, guild_id)`. Prefer the newest matching row in that guild.

### Resolution

Start, progress, finish, and progress lookups use `_latest_progress_row` keyed by guild. `action_add_book` / `action_add_to_wishlist` go through `action_wishlist_add`.

## BCB-008 — `add_book` bypasses wishlist cap and guild filter

**Status:** Resolved 2026-08-31
**Found:** 2026-08-30 code review

### Symptom

“Add Ender's Game to my shelf” hits `action_add_book`, which inserts `status='wishlist'` with no 25-cap and no guild filter. The dedicated wishlist intent uses `action_wishlist_add` (cap + guild).

### Fix

Route `add_book` through `action_wishlist_add` (or the same guild + cap checks).

### Resolution

Chat `add_book` and `add_to_wishlist` both call `action_wishlist_add`. Direct `action_add_book` is a bool wrapper around the same helper.

## BCB-009 — Overlapping `on_ready` can double-close polls

**Status:** Resolved 2026-09-02
**Found:** 2026-08-30 code review

### Symptom

Enrollment sync is locked because overlapping `on_ready` is treated as real. Poll auto-close jobs are created without a stable id. Two overlapping `schedule_poll_auto_close` calls both `add_job`; a second `close_poll` with nothing open still posts “No one voted.” `announce_current_release` is check-then-send-then-record and can double-post on two ready events.

### Fix

One `on_ready` lock; auto-close jobs with `id=f"auto-close:{kind}:{guild_id}"` and `replace_existing=True`; no-op if nothing was open; record a release announcement before send (or UNIQUE insert first).

### Resolution

`on_ready` takes `jobs.ready_lock()`. Nomination, book-poll, and coordination auto-close jobs use `auto-close:{kind}:{guild_id}` with `replace_existing=True` and launch the shared close workflows, which no-op when nothing is open. Release announcements keep the in-flight guard and persist only after a successful send so interrupted delivery can retry.

---

## BCB-001 — Year-in-review recovery posts on every post-January restart

**Status:** Resolved 2026-08-26
**Observed:** 2026-08-25
**Environment:** Production Docker deployment

### Symptom

After the bot restarted on August 25, it posted a new **Year in review** message even though the scheduled annual job is configured for January 1 at 09:00. Discord bots are expected to remain silent on startup/restart; startup recovery must not create routine user-facing announcements.

### Root cause

`bot.py:on_ready()` always calls:

```python
await recover_missed_year_in_review()
```

`jobs.py:recover_missed_year_in_review()` treated the absence of a current-year `year_in_review` event as proof that the January 1 job was missed. Because the current date was after January 1, any restart before the first annual event existed triggered `run_year_in_review()` immediately.

The scheduled job itself is registered as an APScheduler cron job:

```python
month=1, day=1, hour=9
```

The recovery check had no narrow missed-run window and no distinction between:

- a legitimate outage near the scheduled execution time; and
- the normal first startup of the year months after January 1.

### Production evidence

- The container restarted at approximately `2026-08-25 23:21 UTC`.
- Startup registered `schedule_year_in_review`.
- The current-year event was absent before recovery.
- The recovery path generated the post and then logged:

```json
{"year": 2026}
```

### Resolution

Recovery now runs only during the 24 hours after January 1 at 09:00, matching the scheduled job's misfire window. It recovers guilds individually, records the target year explicitly, preserves per-guild idempotency, and remains silent on ordinary restarts later in the year.

Regression coverage verifies:

1. Startup in August with no current-year event → **no post**.
2. Startup shortly after January 1 when the scheduled run was missed → recovery may run once.
3. Existing current-year event → no duplicate post.
4. Multiple `on_ready` events/reconnects → no duplicate post.

### Related behavior

`on_ready()` also calls `sync_enrollment_panel()`. That routine is intended to edit the persisted enrollment panel rather than create a duplicate. The production database retained the existing enrollment message ID (created August 20), so the enrollment panel was not proven to be a newly created message during this incident.
