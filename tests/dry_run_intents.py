"""
dry_run_intents.py — feed dummy chat messages through the real intent parser
and write the planned actions to a file. No Discord bot, token, or database.

For each message it runs the same path on_message uses up to dispatch:
llm.get_intents() (one live LLM call) → handlers.plan_dispatch(), then records
each planned action with the handler it would reach and its gating flags.
Handlers are NOT run, so nothing is written to the DB or sent anywhere.

⚠️  OPT-IN — one real API call per message. Requires .env with ANTHROPIC_API_KEY
(or whatever LLM_PROVIDER in config.py expects). Never run in CI.

Usage:
    python tests/dry_run_intents.py                                  # bundled sample cases
    python tests/dry_run_intents.py my_messages.json                 # your own cases
    python tests/dry_run_intents.py my_messages.txt                  # one message per line
    python tests/dry_run_intents.py -m "finished Dune, rate it 9"    # ad-hoc message(s)
    python tests/dry_run_intents.py --out data/run1.json

Input formats:
- .json: {"default_context": {...}, "cases": [{"message": ..., "expect": [...], "context": {...}}]}
  or a plain list of message strings. "expect" is an optional ordered list of actions.
- .txt: one message per line; blank lines and lines starting with # are skipped.

Output (default data/dry_run_intents.json, gitignored): one record per message with
the raw parsed intents, the planned dispatch, and pass/fail against "expect".
Exit code is 1 if any case with "expect" didn't match.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

DEFAULT_INPUT = os.path.join(_HERE, "fixtures", "multi_intent_messages.json")
DEFAULT_OUTPUT = os.path.join(_ROOT, "data", "dry_run_intents.json")


def load_cases(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        if path.endswith(".json"):
            data = json.load(f)
        else:
            data = [line.strip() for line in f if line.strip() and not line.lstrip().startswith("#")]
    if isinstance(data, list):
        data = {"cases": data}
    default_context = data.get("default_context", {})
    cases = []
    for case in data["cases"]:
        if isinstance(case, str):
            case = {"message": case}
        cases.append({
            "message": case["message"],
            "expect": case.get("expect"),
            "context": case.get("context", default_context),
        })
    return cases


def dry_run(cases: list[dict], get_intents=None) -> list[dict]:
    """Parse and plan each case. get_intents is injectable so CI can test this offline."""
    import llm
    from handlers import describe_dispatch, plan_dispatch

    get_intents = get_intents or llm.get_intents
    records = []
    for case in cases:
        intents = get_intents(case["message"], dict(case["context"]))
        planned = plan_dispatch(intents)
        actions = [intent["action"] for intent in planned]
        record = {
            "message": case["message"],
            "planned": [
                {"action": i["action"], "args": i["args"], "response": i["response"], **describe_dispatch(i)}
                for i in planned
            ],
            "flag_injection": any(i.get("flag_injection") for i in intents),
            "parsed": intents,
        }
        if case.get("expect") is not None:
            record["expect"] = case["expect"]
            record["pass"] = actions == case["expect"]
        records.append(record)
    return records


def write_report(records: list[dict], out_path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    checked = [r for r in records if "pass" in r]
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "summary": {
            "messages": len(records),
            "checked": len(checked),
            "passed": sum(r["pass"] for r in checked),
            "multi_action": sum(len(r["planned"]) > 1 for r in records),
        },
        "results": records,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _print_summary(records: list[dict]) -> None:
    for r in records:
        mark = {True: "✅", False: "❌"}.get(r.get("pass"), "•")
        print(f"{mark} {r['message'][:70]}")
        for step in r["planned"]:
            print(f"     → {step['action']:<24} {json.dumps(step['args'], ensure_ascii=False)}")
        if r.get("pass") is False:
            print(f"     expected: {r['expect']}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("input", nargs="?", default=DEFAULT_INPUT, help="cases file (.json or .txt)")
    parser.add_argument("-m", "--message", action="append", help="ad-hoc message; repeatable; overrides input file")
    parser.add_argument("--out", default=DEFAULT_OUTPUT, help="where to write the JSON report")
    args = parser.parse_args(argv)

    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, ".env"))
    provider = os.getenv("LLM_PROVIDER", "anthropic")
    key_var = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "deepseek": "DEEPSEEK_API_KEY"}.get(provider)
    if key_var and not os.getenv(key_var):
        print(f"{key_var} is not set (LLM_PROVIDER={provider}). Add it to .env; every message would parse as 'unknown'.")
        return 2

    if args.message:
        default_context = load_cases(DEFAULT_INPUT)[0]["context"] if os.path.exists(DEFAULT_INPUT) else {}
        cases = [{"message": m, "expect": None, "context": default_context} for m in args.message]
    else:
        cases = load_cases(args.input)

    print(f"Dry-running {len(cases)} message(s) through the live intent parser...")
    records = dry_run(cases)
    write_report(records, args.out)
    _print_summary(records)
    print(f"\nWrote {args.out}")
    return 1 if any(r.get("pass") is False for r in records) else 0


if __name__ == "__main__":
    sys.exit(main())
