# Goodreads CSV import and export

**Status: shipped.** `/import` landed 2026-09-15. `/export` (personal shelf
CSV for Goodreads / StoryGraph / Hardcover) landed 2026-09-28. StoryGraph,
Hardcover, and TBR *import* remain later. See
[known-gaps.md](known-gaps.md).

Drafted 2026-09-15.

## Goal

Let a member upload a Goodreads library CSV so their **finished-book
history** lands in this guild. Recommendations and personal shelf queries
stop looking empty on day one.

This is personal read history, not a Goodreads clone and not a TBR sync.

## Why this slice

The research note asked for Goodreads / StoryGraph / Hardcover CSV import,
idempotent and previewed, with duplicate-edition and privacy behavior
defined first. The expensive parts are TBR caps, currently-reading
collisions, ISBN/edition identity, Google Books per row, and dumping
hundreds of personal finishes into `/history`.

Importing only `Exclusive Shelf = read` removes those. Book identity stays
title-only, matching `get_or_create_book` today. Club archive and
leaderboard stay about books logged in Discord.

## Non-goals (v1)

- StoryGraph or Hardcover
- To-read / TBR (wishlist and private queue are capped at 25)
- Currently-reading (must not overwrite a live club book)
- DNF (Goodreads has no first-class DNF field)
- ISBN, edition matching, covers, tags, moods, review text
- Google Books lookup during import
- Natural-language “import my Goodreads” (file upload is a slash command)
- Natural-language export delivery. Chat requests (`export_shelf`) only
  point the member to `/export`, so no shelf or queue titles reach a
  public channel
- Per-book include/exclude UI (Discord selects max out at 25)

## Member flow

1. On Goodreads desktop: **My Books → Import and export → Export Library**.
   Download `goodreads_library_export.csv`.
2. In the club server, run `/import` and attach that file.
3. The bot defers, parses with stdlib `csv`, and replies **ephemerally**
   with counts, for example: 142 finished to add, 8 already on the shelf,
   90 to-read ignored, 3 currently-reading ignored.
4. One **Import** button applies the parsed rows. The View holds the rows
   in memory for a few minutes (same pending-confirm idea as the admin
   dashboard). If the bot restarts, run `/import` again.

Copy must say that this fuels recommendations and the personal shelf, and
that `/history` / recaps / the leaderboard will not become their Goodreads.

Detect Goodreads by headers (`Title`, `Exclusive Shelf`). Any other CSV:
“This isn’t a Goodreads export.”

## Access

Same gate as `/queue`: server only, not a guest, and Tracking if member
opt-in is on.

## Mapping

| Goodreads | This bot |
|-----------|----------|
| `Exclusive Shelf` = `read` | `reading_progress.status = 'finished'` |
| Other exclusive shelves | Counted in the preview, not written |
| `Title` | Required; skip blank rows |
| `Author` | Optional; stored on insert if the title is new |
| `My Rating` 1–5 | `ratings.score = rating * 2`; skip 0; do not overwrite an existing rating |
| `Date Read`, else `Date Added` | `started_at` and `finished_at` |
| ISBN (`="978…"`), pages, shelves, reviews | Ignored |

Hard cap around 2,000 `read` rows per upload so a huge file cannot stall
the interaction.

## Write path

Do **not** call `action_finish_book` or `action_rate_book`. Those stamp
`finished_at = now`, fire achievements, and look up Google Books.

Bulk SQL in one transaction:

- `get_or_create_book(..., enrich=False)` so new titles skip Google Books.
  Reuse the current `LOWER(title)` match; do not invent ISBN uniqueness
  in this slice.
- Insert `reading_progress` with `status = 'finished'`,
  `source = 'goodreads'` (column already exists), dates from the CSV, and
  **`last_activity_at` null** so streaks and nudges stay about live
  logging.
- Skip if this user already has any progress row for that title in this
  guild.
- Insert a rating only when `My Rating` is 1–5 and no rating exists for
  that user and book. Tag it `source = 'goodreads'` and `rated_at` from
  Date Read (else Date Added) so recaps and club averages do not inherit
  the Goodreads score after a later Discord re-read. A Discord `/rate`
  overwrites that row and clears `source`.
- Live start or DNF of an imported title inserts a new non-`goodreads`
  row. It must not `UPDATE` the imported finish.
- Do not run `check_achievements`.

Re-uploading the same export is a no-op for titles already imported.

## Club surfaces

`/history` is every finished book in the guild, not “club reads.” A naive
import would bury the archive and inflate the leaderboard.

| Surface | Imported `source = 'goodreads'` rows |
|---------|--------------------------------------|
| Recommendations, personal shelf, taste profile | Include |
| `/history`, weekly recap, year-in-review | Exclude |
| Leaderboard | Exclude |
| Achievements and streaks | Exclude (no achievement pass; null `last_activity_at`) |

Filter club queries with `source IS NULL OR source != 'goodreads'`. That
is the privacy story: history is personal fuel, not a public dump.

## Implementation sketch

Keep it dependency-light. No new LLM action unless help copy needs a
pointer.

| Piece | Where |
|-------|--------|
| `/import` + `Attachment` option | `commands.py`, ephemeral, Tracking/guest gate like `/queue` |
| `/export` | `commands.py` + `build_shelf_export`; ephemeral CSV attachment |
| Parse + apply | new `actions_import.py` (stdlib `csv`) |
| Confirm button | a small View; parsed rows on the instance; `SafeUIMixin` |
| Skip Google Books | `get_or_create_book(..., enrich=True)` default; import passes `False` |
| Club-query filter | `get_club_history`, recap/year-in-review, `get_leaderboard_stats` |
| Help | `/help` books card: Goodreads export steps + `/import` + `/export` |

`reading_progress.source` is enough for finishes. Ratings need
`ratings.source` (migration 11) so club averages stay Discord-only.

## Tests

Fixture CSV with a `read` row, a `to-read` row, a `currently-reading` row,
a blank title, rating 0, rating 5, an already-finished title, and a
non-Goodreads header.

Assert: only `read` rows write; skip/ignore counts; no Google Books call;
no achievements; club history and leaderboard omit imported rows; personal
history and recommend context include them; second apply is idempotent.

Export tests: empty shelf (embed, no file); finished / reading / wishlist /
queue / DNF mapping; live row winning over a Goodreads duplicate; guild and
user isolation; roundtrip through `parse_goodreads_csv`; slash registration
and guest/Tracking/DM gating.

## Export (`/export`)

Shipped 2026-09-28. Reverse of `/import`: a member downloads their shelf in
this server as a Goodreads-shaped CSV they can upload to Goodreads,
StoryGraph, or Hardcover.

Slash-only, same access as `/import` (server, not a guest, Tracking if
opt-in). The reply is ephemeral and attaches `book-club-shelf.csv`. There
is no chat intent: a public reply would leak private queue titles, and
Discord file delivery is a slash-command path.

An empty shelf gets the embed and no file.

### Mapping

| This bot | Goodreads CSV |
|----------|---------------|
| `finished` | Exclusive Shelf `read` |
| `reading` | Exclusive Shelf `currently-reading` |
| `wishlist` | Exclusive Shelf `to-read` |
| Private queue title not already on the shelf | Exclusive Shelf `to-read` |
| `dnf` | Exclusive Shelf `read`, Bookshelves `dnf` |
| Rating 1–10 | `My Rating` 1–5 via `(score + 1) // 2`; unrated is `0` |
| `finished_at` / `started_at` | `Date Read` / `Date Added` as `YYYY/MM/DD` |
| ISBN / ISBN13 | Empty (not stored) |
| Duplicate title (live row + Goodreads import) | One row; the live (non-`goodreads`) row wins |

Hard cap 2,000 rows, same as import. Encoding is UTF-8 with BOM so Excel
and Goodreads accept the file.

DNF has no first-class Goodreads shelf. `/import` skips `read` rows whose
Bookshelves include `dnf`, so re-importing this bot's export does not turn
DNFs into finishes. Other sites may still count them as read; the embed
notes the DNF count.

Titles and authors starting with `=`, `+`, `-`, or `@` are written with a
leading `'` so spreadsheet apps show them as text instead of evaluating a
formula. `/import` strips that quote again.

### Privacy

Copy on the embed: this is the caller's personal shelf in this server;
private queue titles are included as to-read; uploading the file to
another site shares those titles there.

## Later, only if asked

- StoryGraph parser (emailed CSV, native DNF, quarter-stars)
- Optional “first 25 to-read into the private queue”
- ISBN column and edition matching
- Natural-language help that detects an attached CSV

Reading sprints, covers, and per-shelf visibility stay behind this
surface, same order as [known-gaps.md](known-gaps.md).
