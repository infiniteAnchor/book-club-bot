"""test_intents_replay.py — CI-safe replay of captured intent fixtures.

Replays tests/fixtures/intent_cases.json through llm.get_intent() with the LLM
call mocked, so it runs in CI with zero API calls. It guards three things:

1. Plumbing regression — get_intent still parses/validates the recorded raw
   output into the recorded intent (catches changes in fence-stripping,
   json parsing, or validate_intent).
2. Golden validity — every recorded intent still maps to its expected action,
   so a bad fixture can't be committed silently.
3. Coverage — the fixture file covers the shared cases except explicitly
   tracked examples still awaiting a real-provider capture.

If tests/fixtures/intent_cases.json is missing, run (requires .env with an
ANTHROPIC_API_KEY):  python tests/capture_intent_fixtures.py
"""
import json
import os
import sys
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import llm
from intent_cases import CASES, UNCAPTURED_CASES

FIXTURE_PATH = os.path.join(_HERE, "fixtures", "intent_cases.json")
EXPECTED = {message: action for message, action, _ctx in CASES}
KNOWN_UNCAPTURED = {message for message, _action, _ctx in UNCAPTURED_CASES}


def load_cases():
    if not os.path.exists(FIXTURE_PATH):
        raise AssertionError(
            "tests/fixtures/intent_cases.json is missing. "
            "Run 'python tests/capture_intent_fixtures.py' (needs .env with "
            "ANTHROPIC_API_KEY) and commit the generated fixture file."
        )
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        return json.load(f)["cases"]


class IntentReplayTests(unittest.TestCase):

    def test_fixture_covers_all_cases(self):
        recorded = {case["message"] for case in load_cases()}
        self.assertTrue(recorded <= set(EXPECTED), "fixture contains cases absent from intent_cases.py")
        missing = set(EXPECTED) - recorded
        self.assertTrue(
            missing <= KNOWN_UNCAPTURED,
            f"fixture is missing untracked cases: {sorted(missing - KNOWN_UNCAPTURED)}",
        )
        self.assertTrue(KNOWN_UNCAPTURED <= set(EXPECTED), "uncaptured-case list contains stale entries")

    def test_recorded_actions_match_expected(self):
        for case in load_cases():
            with self.subTest(message=case["message"]):
                self.assertEqual(
                    case["intent"]["action"],
                    EXPECTED[case["message"]],
                    "golden fixture drifted from intent_cases.py — recapture fixtures",
                )

    def test_replay_matches_recorded_intents(self):
        for case in load_cases():
            with self.subTest(message=case["message"]):
                with mock.patch("llm._call_llm", return_value=case["raw"]):
                    result = llm.get_intent(case["message"], case["context"])
                self.assertEqual(result, case["intent"])


if __name__ == "__main__":
    unittest.main()
