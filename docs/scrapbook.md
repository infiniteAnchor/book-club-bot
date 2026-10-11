# Club scrapbook

**Status: shipped 2026-09-25.** Member guide for the live feature. Automatic
recap posting, message bookmarking, and reaction ranking remain later; see
[known-gaps.md](known-gaps.md).

Each shared reading session has a scrapbook for explicitly saved quotes,
highlights, reactions, rating notes, and discussion memories. It remains
available after the session is completed. Rereading the same book creates a
separate scrapbook.

## Member use

- `/scrapbook` opens the current session, or the most recent completed session
  when no read is active. The recap shows dates, saved moments, finish count,
  and average club rating. Next/Previous browse the saved moments.
- `/scrapbook action:Sessions` lists session numbers, newest first. Use
  `session_id:` to open or add to a particular read; `title:` selects the most
  recent session for an exact book title, ignoring case. An explicit session
  number takes precedence over the title and never falls back to another read.
- `/scrapbook action:Add content:… kind:Quote` saves text without posting it in
  the channel. The default kind is Highlight. Entries are limited to 1,000
  characters and each session to 100 entries.
- `/scrapbook action:Remove entry_id:12` removes only the caller's own entry.
  Entry numbers are shown while browsing.
- Chat equivalents: `Save a highlight to the scrapbook: The desert imagery
  stayed with me`, `Show the scrapbook for Dune`, `List scrapbook sessions`,
  and `Remove my scrapbook entry 12`.

Scrapbooks are **shared with club readers**, not private personal notes. All
slash responses and revealed pages are ephemeral because they may contain
spoilers. Chat browse requests produce a button for the requesting reader;
save/remove acknowledgements do not repeat the saved text. Text that someone
types into public chat is still public; use the slash command to submit it
privately. Nothing is automatically collected from Discord messages or posted
when a session ends.

Access follows the existing Tracking enrollment requirement when opt-in is
enabled, and guests cannot use scrapbook. Buttons recheck enrollment, guild,
and the requesting reader. The recap's ratings and finish counts reflect
existing club activity for the book across reads, not a frozen session
snapshot. Goodreads-imported finishes and ratings are excluded. Rating notes
are saved text and do not change numeric ratings.

## Implementation and validation

Migration 12 adds `scrapbook_entries` and its session index; it does not rewrite
existing rows. Standard startup applies it through the ordered migration
runner. `actions_scrapbook.py` owns validation, selection, persistence, ownership,
and aggregate queries. `views_scrapbook.py` owns ephemeral presentation.

Offline tests cover migration persistence, session/guild boundaries, input
limits, owner removal, import exclusion, Discord registration, public reply
content, and interaction gates. Synthetic intent tests verify parser plumbing;
live-provider classification and an actual Discord interaction still require
manual testing. No extra provider calls are needed to build a scrapbook.

Automatic recap posting, message-context bookmarking, reaction voting/ranking,
and LLM-generated summaries are outside this first version.
