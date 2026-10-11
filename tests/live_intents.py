"""
live_intents.py — LLM intent parsing regression suite (LIVE).

Sends natural language phrases through get_intent() and asserts each maps to
the expected action. Burns real API credits (~40 calls).

⚠️  OPT-IN ONLY — never run in CI. Requires .env with ANTHROPIC_API_KEY.
Run before system prompt changes or before shipping a new phase.

Usage: python tests/live_intents.py
"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from llm import get_intent
from intent_cases import CASES


# ── Runner ───────────────────────────────────────────────────────────────────

print(f"Running {len(CASES)} intent cases (LIVE API)...")

passed = 0
failed = 0
failures = []

for msg, expected, ctx in CASES:
    result = get_intent(msg, ctx)
    actual = result.get("action")
    ok = actual == expected
    if ok:
        passed += 1
        print(f"  ✅ [{expected}] {msg[:60]}")
    else:
        failed += 1
        print(f"  ❌ [{expected}] {msg[:60]}")
        print(f"       got: {actual}")
        failures.append((msg, expected, actual))

total = passed + failed
pct = round(passed / total * 100)

print(f"\n{'━' * 50}")
print(f"PASSED {passed}/{total} ({pct}%)")

if failures:
    print("\nFailures:")
    for msg, expected, actual in failures:
        print(f'  • "{msg}"')
        print(f"    expected={expected}, got={actual}")

if pct < 90:
    print("\n⚠️  Match rate below 90% — review system prompt before shipping.")

sys.exit(0 if failed == 0 else 1)
