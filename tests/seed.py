"""
seed.py — Pre-populate the test DB with realistic multi-user data.

Run this once before a manual Discord testing session so the bot
has interesting data to work with from the start.

Usage: python tests/seed.py
Uses the real data/bookclub.db (same DB the bot uses).
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import init_db
from actions import (
    action_add_book, action_start_book, action_finish_book,
    action_rate_book, action_add_to_wishlist, action_dnf_book,
)
from actions_nominations import submit_nomination, open_nomination_window
from actions_social import action_set_goal, update_user_taste_profile

# ── Fake users ───────────────────────────────────────────────────────────────
# Use placeholder discord IDs that won't clash with real users.
# Replace with real Discord IDs if you want them linked to actual accounts.

GUILD = os.getenv("TEST_GUILD_ID", "YOUR_TEST_GUILD_ID")

USERS = {
    "sam":   {"id": "800000000000000001", "name": "Sam"},
    "riley": {"id": "800000000000000002", "name": "Riley"},
    "jordan":{"id": "800000000000000003", "name": "Jordan"},
}

def u(name):
    return USERS[name]["id"], USERS[name]["name"]

print("Seeding database...\n")
init_db()

# ── Sam: avid reader, finished 3 books ───────────────────────────────────────
print("Sam: reading history...")
uid, uname = u("sam")

action_finish_book(uid, uname, GUILD, {"title": "Dune", "author": "Frank Herbert"})
action_rate_book(uid, uname, {"title": "Dune", "score": 10})

action_finish_book(uid, uname, GUILD, {"title": "Foundation", "author": "Isaac Asimov"})
action_rate_book(uid, uname, {"title": "Foundation", "score": 9})

action_finish_book(uid, uname, GUILD, {"title": "The Left Hand of Darkness", "author": "Ursula K. Le Guin"})
action_rate_book(uid, uname, {"title": "The Left Hand of Darkness", "score": 8})

action_start_book(uid, uname, GUILD, {"title": "Neuromancer", "author": "William Gibson"})
action_set_goal(uid, uname, {"goal": 12})
update_user_taste_profile(uid, "Loves sprawling sci-fi epics with big ideas. Gives top marks to world-building. Tends to avoid anything too bleak.")
print("  ✅ Sam seeded (3 finished, 1 reading, goal=12, taste profile)")


# ── Riley: casual reader, one DNF ────────────────────────────────────────────
print("Riley: reading history...")
uid, uname = u("riley")

action_finish_book(uid, uname, GUILD, {"title": "The Martian", "author": "Andy Weir"})
action_rate_book(uid, uname, {"title": "The Martian", "score": 8})

action_finish_book(uid, uname, GUILD, {"title": "Project Hail Mary", "author": "Andy Weir"})
action_rate_book(uid, uname, {"title": "Project Hail Mary", "score": 9})

action_add_book(uid, uname, GUILD, {"title": "Moby Dick", "author": "Herman Melville"})
action_start_book(uid, uname, GUILD, {"title": "Moby Dick"})
action_dnf_book(uid, uname, GUILD, {"title": "Moby Dick"})

action_add_to_wishlist(uid, uname, GUILD, {"title": "Ender's Game", "author": "Orson Scott Card"})
action_set_goal(uid, uname, {"goal": 6})
print("  ✅ Riley seeded (2 finished, 1 DNF, 1 wishlist, goal=6)")


# ── Jordan: brand new, just started ──────────────────────────────────────────
print("Jordan: reading history...")
uid, uname = u("jordan")

action_start_book(uid, uname, GUILD, {"title": "The Name of the Wind", "author": "Patrick Rothfuss"})
action_add_to_wishlist(uid, uname, GUILD, {"title": "The Way of Kings", "author": "Brandon Sanderson"})
action_add_to_wishlist(uid, uname, GUILD, {"title": "Snow Crash", "author": "Neal Stephenson"})
print("  ✅ Jordan seeded (1 reading, 2 wishlist)")


# ── Open nomination window with 2 nominations ────────────────────────────────
print("\nOpening nomination window...")
uid_sam, uname_sam = u("sam")
uid_riley, uname_riley = u("riley")

window_id = open_nomination_window(GUILD, uid_sam, days=5)
if window_id:
    submit_nomination(uid_sam, uname_sam, GUILD, "A Fire Upon the Deep", "Vernor Vinge", "Wild ideas, sprawling scope — feels like something we'd all enjoy")
    submit_nomination(uid_riley, uname_riley, GUILD, "The Long Way to a Small Angry Planet", "Becky Chambers", "Cozy crew vibes, perfect for a group read")
    print(f"  ✅ Nomination window open (id={window_id}) with 2 nominations")
else:
    print("  ⚠️  Could not open nomination window (one may already be open)")


# ── Done ─────────────────────────────────────────────────────────────────────
print(f"""
Done! The bot's DB now has:
  • 3 users: Sam (avid), Riley (casual), Jordan (new)
  • Sam: 3 finished + taste profile + goal of 12
  • Riley: 2 finished + 1 DNF + goal of 6
  • Jordan: 1 reading + 2 wishlist
  • Open nomination window with 2 nominations

Try these in Discord:
  "what's everyone reading"
  "show the leaderboard"
  "list nominations"
  "recommend me something"  (as Sam — will use taste profile)
  "show my reading goal"    (as Sam or Riley)

Note: Sam/Riley/Jordan won't respond to your Discord messages since
their IDs are fake. Use your real account to interact with the bot
normally — the seeded data shows up in group queries and leaderboards.
""")
