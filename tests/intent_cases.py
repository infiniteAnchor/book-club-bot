"""Shared intent test cases for the book club bot.

Single source of truth for the natural-language cases, used by:

- tests/live_intents.py              — opt-in live regression vs the real LLM API (~40 calls)
- tests/capture_intent_fixtures.py   — records live outputs into tests/fixtures/intent_cases.json
- tests/test_intents_replay.py       — CI-safe replay of the golden fixtures

A case is a tuple: (message, expected_action, context).
"""

CONTEXT = {
    "books": [
        {"title": "Dune", "author": "Frank Herbert", "status": "reading", "progress_raw": 150, "progress_unit": "pages"},
    ]
}

EMPTY_CONTEXT = {}

# Newly added queue and scrapbook examples have not yet been run through the opt-in provider
# capture. Keep these listed until real outputs are captured; replay coverage
# permits only these known missing records, while still rejecting unknown or
# untracked omissions. The live suite and capture tool both include them via
# CASES.
UNCAPTURED_CASES = [
    ("Show the scrapbook for Dune", "scrapbook", EMPTY_CONTEXT),
    ("List scrapbook sessions", "scrapbook", EMPTY_CONTEXT),
    ("Save a highlight to the scrapbook: The desert imagery stayed with me", "scrapbook_add", EMPTY_CONTEXT),
    ("Remove my scrapbook entry 12", "scrapbook_remove", EMPTY_CONTEXT),
    ("Put Piranesi on my private reading queue", "queue_add", EMPTY_CONTEXT),
    ("Save The Hobbit to my private TBR", "queue_add", EMPTY_CONTEXT),
    ("Remove Piranesi from my queue", "queue_remove", EMPTY_CONTEXT),
    ("Take The Hobbit off my private reading list", "queue_remove", EMPTY_CONTEXT),
    ("Show me my private TBR", "queue_list", EMPTY_CONTEXT),
    ("What's on my reading queue?", "queue_list", EMPTY_CONTEXT),
    ("Export my shelf", "export_shelf", EMPTY_CONTEXT),
    ("Can I download my books as a CSV for StoryGraph?", "export_shelf", EMPTY_CONTEXT),
    ("Take The Left Hand of Darkness off my wishlist", "remove_from_wishlist", EMPTY_CONTEXT),
    ("Remove The Name of the Wind from my wishlist", "remove_from_wishlist", EMPTY_CONTEXT),
]

# (message, expected_action, context)
CASES = [
    # Book tracking
    ("I finished Dune last night",                          "finish_book",      CONTEXT),
    ("Just finished reading The Road",                      "finish_book",      EMPTY_CONTEXT),
    ("I'm on page 200 of Dune",                            "update_progress",  CONTEXT),
    ("Updated my progress — halfway through Foundation",    "update_progress",  EMPTY_CONTEXT),
    ("Add Ender's Game to my shelf",                       "add_book",         EMPTY_CONTEXT),
    ("Start reading Neuromancer",                          "start_book",       EMPTY_CONTEXT),
    ("Add The Name of the Wind to my wishlist",            "add_to_wishlist",  EMPTY_CONTEXT),
    ("I want to read Snow Crash someday",                  "add_to_wishlist",  EMPTY_CONTEXT),
    *UNCAPTURED_CASES,
    ("Rate Dune a 9 out of 10",                            "rate_book",        CONTEXT),
    ("Give Foundation 7/10",                               "rate_book",        EMPTY_CONTEXT),

    # DNF
    ("I gave up on Moby Dick, it's not for me",            "dnf_book",         EMPTY_CONTEXT),
    ("Abandoning The Silmarillion, too dense",             "dnf_book",         EMPTY_CONTEXT),
    ("DNF on War and Peace",                               "dnf_book",         EMPTY_CONTEXT),

    # Queries
    ("What am I currently reading?",                       "query",            CONTEXT),
    ("Show my finished books",                             "query",            CONTEXT),
    ("What's everyone reading?",                           "group_query",      EMPTY_CONTEXT),
    ("Show everyone's shelf",                              "group_query",      EMPTY_CONTEXT),

    # Recommendations
    ("What should I read next?",                           "recommend",        CONTEXT),
    ("Recommend something similar to Dune",                "recommend",        CONTEXT),
    ("Give me something light and fun",                    "recommend",        EMPTY_CONTEXT),
    ("I want something that will wreck me emotionally",    "recommend",        EMPTY_CONTEXT),

    # Goals & leaderboard
    ("I want to read 20 books this year",                  "set_goal",         EMPTY_CONTEXT),
    ("My reading goal is 12 books",                        "set_goal",         EMPTY_CONTEXT),
    ("How am I tracking on my reading goal?",              "check_goal",       EMPTY_CONTEXT),
    ("Show the leaderboard",                               "leaderboard",      EMPTY_CONTEXT),
    ("Who's read the most books?",                         "leaderboard",      EMPTY_CONTEXT),

    # Nominations
    ("Nominate The Road by Cormac McCarthy",               "nominate",         EMPTY_CONTEXT),
    ("I nominate Ender's Game because it's a classic",     "nominate",         EMPTY_CONTEXT),
    ("Open nominations for the next book",                 "open_nominations", EMPTY_CONTEXT),
    ("Show the current nominations",                       "list_nominations", EMPTY_CONTEXT),

    # Recap & year in review
    ("Give us a weekly recap",                             "summarize",        EMPTY_CONTEXT),
    ("How was our reading year?",                          "year_in_review",   EMPTY_CONTEXT),

    # Member enrollment preferences
    ("Join the book club",                                 "manage_membership", EMPTY_CONTEXT),
    ("Join the book club and send me nudges",              "manage_membership", EMPTY_CONTEXT),
    ("Turn off reading nudges",                            "manage_membership", EMPTY_CONTEXT),
    ("Opt me out of the book club",                        "manage_membership", EMPTY_CONTEXT),
    ("What are my book club settings?",                    "manage_membership", EMPTY_CONTEXT),

    # Help and discovery
    ("Help",                                               "help",             EMPTY_CONTEXT),
    ("What can you do?",                                   "help",             EMPTY_CONTEXT),
    ("How do polls work?",                                 "help",             EMPTY_CONTEXT),
]

# Synthetic, offline-only parser cases for actions added after the captured
# provider fixtures were recorded. Expected argument payloads are separate
# from the live CASES contexts; these hand-authored payloads must never be
# presented as captured model behavior.
QUEUE_REGRESSION_CASES = [
    # (message, expected_action, expected_args)
    ("Put Piranesi on my private reading queue", "queue_add", {"title": "Piranesi"}),
    ("Remove Piranesi from my queue", "queue_remove", {"title": "Piranesi"}),
    ("Show me my private TBR", "queue_list", {}),
    ("Export my shelf", "export_shelf", {}),
    ("Take The Left Hand of Darkness off my wishlist", "remove_from_wishlist", {"title": "The Left Hand of Darkness"}),
]
