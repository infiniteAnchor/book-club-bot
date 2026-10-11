"""
simulate.py — Book Club Bot logic simulator

Runs all major action flows directly against a temp SQLite DB.
No Discord, no API calls, no accounts needed.

Usage: python tests/simulate.py
"""
import os, sys, sqlite3, asyncio

# Patch DB_PATH before importing anything that touches the DB
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)
os.environ.setdefault("BOOKCLUB_TEST_FAST_SQLITE", "1")
import database
database.DB_PATH = "/tmp/test_bookclub_sim.db"

import actions
import actions_social
from google_books_offline import patch_google_books_offline

patch_google_books_offline().start()

from database import init_db
from actions import (
    action_add_book, action_start_book, action_update_progress,
    action_finish_book, action_rate_book, action_add_to_wishlist,
    action_dnf_book, query_shelf, get_user_reading_history, build_user_context,
)
from actions_nominations import (
    open_nomination_window, submit_nomination, close_nomination_window,
    get_nominations, open_poll, cast_vote, get_poll_results, close_poll,
    open_tiebreaker_poll, open_coordination_poll, cast_coordination_vote,
    close_coordination_poll, get_active_poll, get_active_window,
)
from typing import cast, Any

from actions_social import (
    check_achievements, action_set_goal, get_goal_progress,
    get_leaderboard_stats, get_admin_summary,
    get_member_feature_preferences, set_member_feature_preferences,
    get_inactivity_nudge_groups, deliver_inactivity_nudge,
)
from actions_queue import (
    LIST_CAP,
    action_queue_add, action_queue_list, action_queue_remove,
    action_wishlist_add, action_wishlist_remove, list_next_book_choices, action_start_from_choice,
)
from actions_session import (
    complete_club_read, get_active_session, maybe_start_from_poll_winner,
    set_club_target, start_club_read,
)

# ── Setup ────────────────────────────────────────────────────────────────────

if os.path.exists(database.DB_PATH):
    os.remove(database.DB_PATH)
init_db()

GUILD = "999000000000000001"
ALICE = {"id": "111000000000000001", "name": "Alice"}
BOB   = {"id": "222000000000000002", "name": "Bob"}
CAROL = {"id": "333000000000000003", "name": "Carol"}
DAVE  = {"id": "444000000000000004", "name": "Dave"}

passed = 0
failed = 0

def check(name, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  ✅ {name}")
        passed += 1
    else:
        msg = f" — {detail}" if detail else ""
        print(f"  ❌ {name}{msg}")
        failed += 1

def section(title):
    print(f"\n{title}")
    print("─" * len(title))

def db_query(sql, params=()):
    conn = sqlite3.connect(database.DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        return cur.fetchone()
    finally:
        conn.close()


# ── 1. Book lifecycle ────────────────────────────────────────────────────────

section("1. Book lifecycle (add → start → progress → finish → rate)")

ok = action_add_book(ALICE["id"], ALICE["name"], GUILD, {"title": "Dune", "author": "Frank Herbert"})
check("add_book", ok)

ok = action_start_book(ALICE["id"], ALICE["name"], GUILD, {"title": "Dune"})
check("start_book", ok)

ok = action_update_progress(ALICE["id"], ALICE["name"], GUILD, {"title": "Dune", "progress_raw": 200, "progress_unit": "pages"})
check("update_progress", ok)

ok = action_finish_book(ALICE["id"], ALICE["name"], GUILD, {"title": "Dune"})
check("finish_book", ok)

ok = action_rate_book(ALICE["id"], ALICE["name"], {"title": "Dune", "score": 9}, GUILD)
check("rate_book", ok)


# ── 2. Wishlist → start ──────────────────────────────────────────────────────

section("2. Wishlist → start")

ok = action_add_to_wishlist(BOB["id"], BOB["name"], GUILD, {"title": "The Road", "author": "Cormac McCarthy"})
check("add_to_wishlist", ok)

ok = action_start_book(BOB["id"], BOB["name"], GUILD, {"title": "The Road"})
check("start_book from wishlist", ok)

shelf = query_shelf(BOB["id"], "reading", GUILD)
check("status is reading (not wishlist)", any(b["title"] == "The Road" for b in shelf))


# ── 3. Re-read ───────────────────────────────────────────────────────────────

section("3. Re-read")

ok = action_finish_book(ALICE["id"], ALICE["name"], GUILD, {"title": "Dune"})
check("finish_book again succeeds", ok)

row = db_query(
    "SELECT MAX(read_count) FROM reading_progress rp JOIN books b ON b.book_id = rp.book_id WHERE b.title = 'Dune'"
)
check("read_count incremented to 2", row and row[0] == 2, f"got {row[0] if row else None}")


# ── 4. DNF ───────────────────────────────────────────────────────────────────

section("4. DNF")

action_add_book(CAROL["id"], CAROL["name"], GUILD, {"title": "Moby Dick", "author": "Herman Melville"})
action_start_book(CAROL["id"], CAROL["name"], GUILD, {"title": "Moby Dick"})

success, title, author = action_dnf_book(CAROL["id"], CAROL["name"], GUILD, {"title": "Moby Dick"})
check("dnf_book succeeds", success, f"title={title}")

row = db_query(
    "SELECT status FROM reading_progress rp JOIN books b ON b.book_id = rp.book_id WHERE b.title = 'Moby Dick'"
)
check("status is dnf in DB", row and row[0] == "dnf", f"got {row[0] if row else None}")

success2, _, _ = action_dnf_book(CAROL["id"], CAROL["name"], GUILD, {"title": "Moby Dick"})
check("duplicate dnf returns False", not success2)


# ── 5. Achievements ──────────────────────────────────────────────────────────

section("5. Achievements")

# Alice: finished Dune (twice), rated it → should get first_finish, first_rating
alice_ach = check_achievements(ALICE["id"], ALICE["name"], GUILD)
ach_types = {a["type"] for a in alice_ach}
check("first_finish awarded", "first_finish" in ach_types, f"got {ach_types}")
check("first_rating awarded", "first_rating" in ach_types, f"got {ach_types}")

# Carol: DNF'd → dnf_survivor
carol_ach = check_achievements(CAROL["id"], CAROL["name"], GUILD)
ach_types = {a["type"] for a in carol_ach}
check("dnf_survivor awarded", "dnf_survivor" in ach_types, f"got {ach_types}")

# Idempotent — second call returns empty (already earned)
alice_ach2 = check_achievements(ALICE["id"], ALICE["name"], GUILD)
check("achievements are idempotent (no duplicates)", alice_ach2 == [])

# harsh_critic: give a score ≤ 3
action_add_book(BOB["id"], BOB["name"], GUILD, {"title": "A Bad Book"})
action_finish_book(BOB["id"], BOB["name"], GUILD, {"title": "A Bad Book"})
action_rate_book(BOB["id"], BOB["name"], {"title": "A Bad Book", "score": 2}, GUILD)
bob_ach = check_achievements(BOB["id"], BOB["name"], GUILD)
ach_types = {a["type"] for a in bob_ach}
check("harsh_critic awarded for score ≤ 3", "harsh_critic" in ach_types, f"got {ach_types}")


# ── 6. Goal tracking ─────────────────────────────────────────────────────────

section("6. Goal tracking")

ok = action_set_goal(ALICE["id"], ALICE["name"], {"goal": 12})
check("set_goal", ok)

progress = get_goal_progress(ALICE["id"], GUILD)
check("goal is 12", progress.get("goal") == 12, f"got {progress}")
# Alice finished Dune twice = 2 books this year
check("progress reflects finished books", progress.get("progress") == 2, f"got progress={progress.get('progress')}")

# build_user_context includes goal
ctx = build_user_context(ALICE["id"], GUILD)
check("reading_goal in build_user_context", "reading_goal" in ctx, f"keys: {list(ctx.keys())}")


# ── 7. Nominations flow ──────────────────────────────────────────────────────

section("7. Nominations flow")

window_id = open_nomination_window(GUILD, ALICE["id"], days=7)
check("open_nomination_window", window_id > 0, f"got {window_id}")

# Duplicate window rejected
window_id2 = open_nomination_window(GUILD, ALICE["id"], days=7)
check("duplicate window returns 0", window_id2 == 0)

ok, msg = submit_nomination(ALICE["id"], ALICE["name"], GUILD, "Foundation", "Isaac Asimov", "Classic sci-fi")
check("submit_nomination Alice", ok, msg)

ok, msg = submit_nomination(BOB["id"], BOB["name"], GUILD, "Ender's Game", "Orson Scott Card", "Space battles")
check("submit_nomination Bob", ok, msg)

ok, msg = submit_nomination(CAROL["id"], CAROL["name"], GUILD, "The Martian", "Andy Weir", "Fun science")
check("submit_nomination Carol", ok, msg)

ok, msg = submit_nomination(ALICE["id"], ALICE["name"], GUILD, "Foundation 2", "Isaac Asimov", "Duplicate attempt")
check("duplicate nomination rejected", not ok and msg == "already_nominated", f"ok={ok}, msg={msg}")

noms = get_nominations(GUILD)
check("get_nominations returns 3", len(noms) == 3, f"got {len(noms)}")

closed = close_nomination_window(GUILD, manual=True)
check("close_nomination_window returns 3 noms", len(closed) == 3, f"got {len(closed)}")


# ── 8. Poll flow ─────────────────────────────────────────────────────────────

section("8. Poll flow")

poll_id, noms = open_poll(GUILD, ALICE["id"], days=1)
check("open_poll returns id and nominations", cast(int, poll_id) > 0 and len(noms) == 3, f"poll_id={poll_id}, noms={len(noms)}")

nom_map = {n["title"]: n["nomination_id"] for n in noms}

# Alice and Carol → Foundation (2 votes), Bob → Ender's Game, Dave → The Martian
ok, msg = cast_vote(ALICE["id"], ALICE["name"], GUILD, nom_map["Foundation"])
check("cast_vote Alice", ok, msg)

ok, msg = cast_vote(BOB["id"], BOB["name"], GUILD, nom_map["Ender's Game"])
check("cast_vote Bob", ok, msg)

ok, msg = cast_vote(CAROL["id"], CAROL["name"], GUILD, nom_map["Foundation"])
check("cast_vote Carol", ok, msg)

ok, msg = cast_vote(DAVE["id"], DAVE["name"], GUILD, nom_map["The Martian"])
check("cast_vote Dave", ok, msg)

# Vote update
ok, msg = cast_vote(DAVE["id"], DAVE["name"], GUILD, nom_map["Foundation"])
check("vote update returns 'updated'", ok and msg == "updated", f"ok={ok}, msg={msg}")

results = get_poll_results(GUILD)
check("get_poll_results non-empty", len(results) > 0)

result = close_poll(GUILD, manual=True)
winner = (result.get("winner") or {}).get("title")
check("winner is Foundation", winner == "Foundation", f"winner={winner}")
check("not tied", not result.get("tied"))
session = maybe_start_from_poll_winner(GUILD, ALICE["id"], result.get("winner"), poll_id)
check("poll winner starts club read", session is not None and session.get("title") == "Foundation")
check("active club book is Foundation", (get_active_session(GUILD) or {}).get("title") == "Foundation")
ok, status, _session = set_club_target(GUILD, ALICE["id"], "2026-12-01")
check("set club target", ok and status == "set")
ok, status, _session = complete_club_read(GUILD, ALICE["id"])
check("complete club read", ok and status == "completed")
ok, status, _session = start_club_read(GUILD, ALICE["id"], {"title": "Dune", "author": "Frank Herbert"})
check("start next club read", ok and status == "started" and (_session or {}).get("title") == "Dune")


# ── 9. Tiebreaker ────────────────────────────────────────────────────────────

section("9. Tiebreaker")

window_id = open_nomination_window(GUILD, ALICE["id"], days=1)
submit_nomination(ALICE["id"], ALICE["name"], GUILD, "Neuromancer", "William Gibson", "Cyberpunk")
submit_nomination(BOB["id"], BOB["name"], GUILD, "Snow Crash", "Neal Stephenson", "More cyberpunk")
close_nomination_window(GUILD, manual=True)

poll_id, noms = open_poll(GUILD, ALICE["id"], days=1)
nom_map = {n["title"]: n["nomination_id"] for n in noms}

cast_vote(ALICE["id"], ALICE["name"], GUILD, nom_map["Neuromancer"])
cast_vote(BOB["id"], BOB["name"], GUILD, nom_map["Snow Crash"])

result = close_poll(GUILD, manual=True)
check("tie detected", result.get("tied") == True, f"tied={result.get('tied')}")

tied_ids = [n["nomination_id"] for n in result.get("tied_nominations", [])]
tb_poll_id, tb_noms = open_tiebreaker_poll(GUILD, ALICE["id"], tied_ids, days=1)
check("tiebreaker poll opens with 2 noms", tb_poll_id > 0 and len(tb_noms) == 2, f"poll_id={tb_poll_id}, noms={len(tb_noms)}")
check("tiebreaker does not leave nominations open", get_active_window(GUILD) is None)

# Close tiebreaker poll and its window before next section
close_poll(GUILD, manual=True)
close_nomination_window(GUILD, manual=True)


# ── 10. Edge case: open poll with no nominations ─────────────────────────────

section("10. Edge case: open_poll with no nominations")

window_id = open_nomination_window(GUILD, ALICE["id"], days=1)
close_nomination_window(GUILD, manual=True)  # close with zero nominations

poll_id, noms = open_poll(GUILD, ALICE["id"], days=1)
check("open_poll returns (0, []) when no nominations", poll_id == 0 and noms == [], f"poll_id={poll_id}, noms={noms}")

row = db_query("SELECT COUNT(*) FROM polls WHERE status = 'open' AND guild_id = ?", (GUILD,))
check("no orphan poll row in DB", row and row[0] == 0, f"found {row[0] if row else '?'} open polls")


# ── 11. Coordination poll ────────────────────────────────────────────────────

section("11. Coordination poll")

cp_id, options = open_coordination_poll(GUILD, ALICE["id"], "When should we meet?", ["Monday", "Wednesday", "Friday"], days=1)
check("open_coordination_poll", cast(int, cp_id) > 0 and len(options) == 3, f"id={cp_id}, opts={len(options)}")

opt_map = {o["label"]: o["option_id"] for o in options}

ok, msg = cast_coordination_vote(ALICE["id"], ALICE["name"], GUILD, opt_map["Monday"])
check("cast_coordination_vote Alice", ok, msg)

ok, msg = cast_coordination_vote(BOB["id"], BOB["name"], GUILD, opt_map["Monday"])
check("cast_coordination_vote Bob", ok, msg)

ok, msg = cast_coordination_vote(CAROL["id"], CAROL["name"], GUILD, opt_map["Wednesday"])
check("cast_coordination_vote Carol", ok, msg)

ok, msg = cast_coordination_vote(ALICE["id"], ALICE["name"], GUILD, opt_map["Wednesday"])
check("coordination vote update", ok and msg == "updated", f"ok={ok}, msg={msg}")

cp_result = close_coordination_poll(GUILD, manual=True)
winner_label = (cp_result.get("winner") or {}).get("label")
check("coordination winner is Wednesday (2 votes)", winner_label == "Wednesday", f"winner={winner_label}")


# ── 12. Leaderboard ──────────────────────────────────────────────────────────

section("12. Leaderboard")

stats = get_leaderboard_stats(GUILD)
check("leaderboard returns entries", len(stats) > 0)

alice_stats = next((s for s in stats if s["name"] == "Alice"), None)
check("Alice in leaderboard", alice_stats is not None)
check("Alice has 2 books finished", alice_stats and alice_stats["books_finished"] == 2, f"got {alice_stats}")


# ── 13. Query functions ──────────────────────────────────────────────────────

section("13. Query functions")

alice_finished = query_shelf(ALICE["id"], "finished", GUILD)
check("query_shelf finished", len(alice_finished) > 0)

history = get_user_reading_history(ALICE["id"], GUILD)
check("get_user_reading_history has finished list", "finished" in history)
check("history includes Dune", any(b["title"] == "Dune" for b in history["finished"]))

ctx = build_user_context(ALICE["id"], GUILD)
check("build_user_context returns books", "books" in ctx and len(ctx["books"]) > 0)


# ── 14. Admin summary ────────────────────────────────────────────────────────

section("14. Admin summary")

summary = get_admin_summary(GUILD)
check("get_admin_summary returns dict", isinstance(summary, dict))
check("total_users > 0", summary.get("total_users", 0) > 0, f"got {summary.get('total_users')}")
check("total_finished > 0", summary.get("total_finished", 0) > 0, f"got {summary.get('total_finished')}")
check("recent_achievements present", "recent_achievements" in summary)


# ── 15. Member feature preferences ──────────────────────────────────────────

section("15. Member feature preferences")

prefs = get_member_feature_preferences(BOB["id"], GUILD)
check(
    "absent preferences default off",
    not any(prefs[key] for key in ("tracking", "updates", "club", "dm_nudges")),
)

prefs = set_member_feature_preferences(BOB["id"], BOB["name"], GUILD, dm_nudges=True)
check("enabling nudges enables tracking", prefs["tracking"] and prefs["dm_nudges"])

prefs = set_member_feature_preferences(BOB["id"], BOB["name"], GUILD, tracking=False)
check(
    "disabling tracking cascades upward",
    not prefs["tracking"] and not prefs["updates"] and not prefs["club"] and not prefs["dm_nudges"],
)

prefs = set_member_feature_preferences(BOB["id"], BOB["name"], GUILD, club=True)
check("club grants updates and tracking", prefs["club"] and prefs["updates"] and prefs["tracking"])

prefs = set_member_feature_preferences(BOB["id"], BOB["name"], GUILD, club=False)
check("removing club preserves updates and tracking", not prefs["club"] and prefs["updates"] and prefs["tracking"])

prefs = set_member_feature_preferences(BOB["id"], BOB["name"], GUILD, updates=True, dm_nudges=True)
check("nudges coexist with updates tier", prefs["updates"] and prefs["dm_nudges"] and not prefs["club"])

groups = get_inactivity_nudge_groups(GUILD, 0, 0)
bob_group = next((group for group in groups if group["discord_id"] == BOB["id"]), None)
check("nudge candidates require tracking and nudges", bob_group is not None)

OTHER_GUILD = "999000000000000002"
prefs = set_member_feature_preferences(BOB["id"], BOB["name"], OTHER_GUILD, tracking=True)
set_member_feature_preferences(BOB["id"], BOB["name"], GUILD, tracking=False)

sent_messages = []
async def successful_send(content):
    sent_messages.append(content)

# deliver_inactivity_nudge forwards *args/**kwargs to send, so a positional
# content string still works here; prod passes embed=make_nudge_embed(group).
sent = asyncio.run(deliver_inactivity_nudge(successful_send, cast(dict[str, Any], bob_group), "check in"))
row = db_query(
    "SELECT last_nudged_at FROM reading_progress rp JOIN users u ON rp.user_id = u.user_id WHERE u.discord_id = ? AND rp.guild_id = ?",
    (BOB["id"], GUILD),
)
check("successful nudge marks progress", sent and sent_messages == ["check in"] and row and row[0] is not None)

conn = sqlite3.connect(database.DB_PATH)
conn.execute("UPDATE reading_progress SET last_nudged_at = NULL WHERE guild_id = ?", (GUILD,))
conn.commit()
conn.close()

async def failed_send(_content):
    raise RuntimeError("DMs closed")

sent = asyncio.run(deliver_inactivity_nudge(failed_send, cast(dict[str, Any], bob_group), "check in"))
row = db_query(
    "SELECT last_nudged_at FROM reading_progress rp JOIN users u ON rp.user_id = u.user_id WHERE u.discord_id = ? AND rp.guild_id = ?",
    (BOB["id"], GUILD),
)
check("failed nudge stays eligible", not sent and row and row[0] is None)


# ── 16. Personal reading queue ───────────────────────────────────────────────

section("16. Personal reading queue")

ok, status = action_queue_add(DAVE["id"], DAVE["name"], GUILD, {"title": "Ancillary Justice", "author": "Ann Leckie"}, source="chat")
check("queue add succeeds", ok and status == "added")
ok, status = action_queue_add(DAVE["id"], DAVE["name"], GUILD, {"title": "Ancillary Justice"}, source="chat")
check("queue dedupes", ok and status == "already")
action_add_to_wishlist(DAVE["id"], DAVE["name"], GUILD, {"title": "The Dispossessed", "author": "Ursula K. Le Guin"})
choices = list_next_book_choices(DAVE["id"], GUILD)
check(
    "combined choices include queue then wishlist",
    [(c["list"], c["title"]) for c in choices] == [
        ("queue", "Ancillary Justice"),
        ("wishlist", "The Dispossessed"),
    ],
)
ok = action_start_from_choice(
    DAVE["id"], DAVE["name"], GUILD, list_name="queue", title="Ancillary Justice", author="Ann Leckie"
)
check("start from queue removes queue row", ok and action_queue_list(DAVE["id"], GUILD) == [])
check(
    "start from queue marks reading",
    any(b["title"] == "Ancillary Justice" for b in query_shelf(DAVE["id"], "reading", GUILD)),
)
ok, status = action_wishlist_remove(DAVE["id"], DAVE["name"], GUILD, {"title": "The Dispossessed"})
check("wishlist remove succeeds", ok and status == "removed")
ok, status = action_queue_remove(DAVE["id"], DAVE["name"], GUILD, {"title": "Missing Book"})
check("queue remove missing is not_found", (not ok) and status == "not_found")


# ── 17. Guild-scoped shelf writes ────────────────────────────────────────────

section("17. Guild-scoped shelf writes")

GUILD_B = "999000000000000002"
action_start_book(ALICE["id"], ALICE["name"], GUILD, {"title": "Hyperion", "author": "Dan Simmons"})
action_update_progress(
    ALICE["id"], ALICE["name"], GUILD, {"title": "Hyperion", "progress_raw": 40, "progress_unit": "pages"}
)
action_start_book(ALICE["id"], ALICE["name"], GUILD_B, {"title": "Hyperion", "author": "Dan Simmons"})
action_finish_book(ALICE["id"], ALICE["name"], GUILD_B, {"title": "Hyperion"})
row_a = db_query(
    "SELECT rp.status, rp.progress_raw FROM reading_progress rp JOIN books b ON b.book_id = rp.book_id WHERE b.title = 'Hyperion' AND rp.guild_id = ?",
    (GUILD,),
)
row_b = db_query(
    "SELECT rp.status FROM reading_progress rp JOIN books b ON b.book_id = rp.book_id WHERE b.title = 'Hyperion' AND rp.guild_id = ?",
    (GUILD_B,),
)
check("progress stays in calling guild", row_a and row_a[0] == "reading" and row_a[1] == 40)
check("finish in other guild is separate", row_b and row_b[0] == "finished")

for i in range(LIST_CAP):
    action_wishlist_add(CAROL["id"], CAROL["name"], GUILD_B, {"title": f"Cap Book {i}"})
ok = action_add_book(CAROL["id"], CAROL["name"], GUILD_B, {"title": "Cap Overflow"})
check("add_book respects wishlist cap", not ok)
check(
    "capped guild stays at 25",
    len(query_shelf(CAROL["id"], "wishlist", GUILD_B)) == LIST_CAP,
)
ok = action_add_book(CAROL["id"], CAROL["name"], GUILD, {"title": "Cap Overflow"})
check("add_book still works in another guild", ok)


# ── Summary ──────────────────────────────────────────────────────────────────

total = passed + failed
print(f"\n{'━' * 40}")
print(f"PASSED {passed}/{total}" + ("  🎉" if failed == 0 else f"  ({failed} failed)"))

os.remove(database.DB_PATH)
sys.exit(0 if failed == 0 else 1)
