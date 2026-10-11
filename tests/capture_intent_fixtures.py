"""
capture_intent_fixtures.py — record real LLM outputs as golden fixtures.

Runs every case from intent_cases.py through the live API, captures the exact
raw text returned by _call_llm plus the validated intent, and writes
tests/fixtures/intent_cases.json. Commit that file: tests/test_intents_replay.py
replays it in CI with zero API calls, and the golden file lets you diff how a
system-prompt change altered real parsing behavior.

⚠️  OPT-IN — hits the real API (~40 calls). Requires .env with ANTHROPIC_API_KEY.
Refresh whenever you change the system prompt:
    python tests/capture_intent_fixtures.py
Then review the git diff of tests/fixtures/intent_cases.json before committing.

Usage: python tests/capture_intent_fixtures.py
"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

import llm
from intent_cases import CASES

FIXTURE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "intent_cases.json")


def capture_case(message, context):
    """Call get_intent, capturing the raw _call_llm text it consumed."""
    raws = []
    original = llm._call_llm

    def spy(system, prompt, max_tokens, **kwargs):
        raw = original(system, prompt, max_tokens, **kwargs)
        raws.append(raw)
        return raw

    llm._call_llm = spy
    try:
        intent = llm.get_intent(message, context)
    finally:
        llm._call_llm = original

    if not raws:
        raise RuntimeError(f"No LLM call made for: {message!r} — API error or guardrail returned early.")
    return raws[0], intent


def main():
    os.makedirs(os.path.dirname(FIXTURE_PATH), exist_ok=True)

    print(f"Capturing {len(CASES)} cases (LIVE API)...")
    cases = []
    for i, (message, expected, context) in enumerate(CASES, 1):
        raw, intent = capture_case(message, context)
        cases.append({
            "message": message,
            "expected_action": expected,
            "context": context,
            "raw": raw,
            "intent": intent,
        })
        print(f"  {i:2d}/{len(CASES)} [{intent.get('action')}] {message[:55]}")

    with open(FIXTURE_PATH, "w", encoding="utf-8") as f:
        json.dump({"cases": cases}, f, indent=2, ensure_ascii=False)
        f.write("\n\n")

    print(f"\nWrote {FIXTURE_PATH} ({len(cases)} cases).")
    print("Review the git diff before committing — this file is the prompt-regression record.")


if __name__ == "__main__":
    main()
