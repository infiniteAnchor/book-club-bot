import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar, copy_context
from functools import partial
from threading import Lock
from time import perf_counter

import telemetry
import json
import re
from config import (
    ANTHROPIC_API_KEY, OPENAI_API_KEY, DEEPSEEK_API_KEY,
    LLM_PROVIDER, LLM_MODEL, SINGLE_CALL_MODE, resolve_llm_model,
)

_clients = {}
_error_handler = None
_client_lock = Lock()
_error_loop = ContextVar("llm_error_loop", default=None)
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="bookclub-llm")
LLM_TIMEOUT_SECONDS = 30.0


async def run(function, *args, **kwargs):
    """Run a synchronous LLM helper without blocking Discord; preserve request context."""
    loop = asyncio.get_running_loop()
    token = _error_loop.set(loop)
    try:
        context = copy_context()
        return await loop.run_in_executor(_executor, context.run, partial(function, *args, **kwargs))
    finally:
        _error_loop.reset(token)



def set_llm_error_handler(handler) -> None:
    """Register a best-effort callback for actionable provider failures."""
    global _error_handler
    _error_handler = handler


def classify_llm_error(error: Exception) -> dict | None:
    """Return safe alert metadata for auth, billing, quota, and rate-limit errors."""
    status_code = getattr(error, "status_code", None) or getattr(error, "status", None)
    error_name = type(error).__name__.lower()
    message = str(error).lower()
    searchable = f"{error_name} {message}"

    if status_code == 401 or any(term in searchable for term in (
        "authentication", "invalid api key", "invalid_api_key", "unauthorized",
    )):
        category = "authentication"
    elif any(term in searchable for term in (
        "insufficient_credit", "insufficient credit", "credit balance", "billing",
        "payment required", "payment_required",
    )):
        category = "credit/billing"
    elif any(term in searchable for term in (
        "insufficient_quota", "quota exceeded", "quota_exceeded", "spend limit",
    )):
        category = "quota"
    elif status_code == 429 or any(term in searchable for term in (
        "rate limit", "rate_limit", "too many requests",
    )):
        category = "rate limit"
    else:
        return None

    return {
        "provider": LLM_PROVIDER,
        "category": category,
        "status_code": status_code,
        "error_type": type(error).__name__,
    }


def _report_llm_error(error: Exception) -> None:
    """Report actionable errors without allowing alert delivery to affect the caller."""
    failure = classify_llm_error(error)
    if failure and _error_handler:
        try:
            loop = _error_loop.get()
            if loop is not None:
                loop.call_soon_threadsafe(_deliver_llm_error, _error_handler, failure)
            else:
                _deliver_llm_error(_error_handler, failure)
        except Exception:
            pass


def _deliver_llm_error(handler, failure):
    try:
        handler(failure)
    except Exception:
        pass


def _get_client():
    with _client_lock:
        return _get_client_locked()


def _get_client_locked():
    provider = LLM_PROVIDER
    if provider in _clients:
        return _clients[provider]
    if provider == "anthropic":
        import anthropic
        _clients[provider] = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=LLM_TIMEOUT_SECONDS, max_retries=0)
    elif provider in ("openai", "deepseek"):
        from openai import OpenAI
        api_key = OPENAI_API_KEY if provider == "openai" else DEEPSEEK_API_KEY
        base_url = "https://api.deepseek.com" if provider == "deepseek" else None
        _clients[provider] = OpenAI(api_key=api_key, timeout=LLM_TIMEOUT_SECONDS, max_retries=0, **({"base_url": base_url} if base_url else {}))
    else:
        raise ValueError(f"Unknown LLM_PROVIDER: {provider}")
    return _clients[provider]


def _anthropic_system(system: str, system_suffix: str, cache_system: bool):
    """Build Anthropic system blocks; the cached base stays byte-identical across suffixes."""
    if not cache_system:
        return system + system_suffix
    blocks = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    if system_suffix:
        blocks.append({"type": "text", "text": system_suffix})
    return blocks


def _call_llm(system: str, prompt: str, max_tokens: int, system_suffix: str = "", cache_system: bool = False) -> str:
    """Call the configured LLM provider and return the text response.

    cache_system marks `system` for Anthropic prompt caching; `system_suffix` is
    sent after the cache breakpoint so per-user overrides still hit the cache.
    """
    provider = LLM_PROVIDER
    model = resolve_llm_model(provider, LLM_MODEL)
    started = perf_counter()
    failed = False
    input_tokens = output_tokens = cache_read_tokens = cache_write_tokens = 0
    try:
        client = _get_client()
        if provider == "anthropic":
            msg = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=_anthropic_system(system, system_suffix, cache_system),
                messages=[{"role": "user", "content": prompt}],
            )
            usage = getattr(msg, "usage", None)
            input_tokens = getattr(usage, "input_tokens", 0)
            output_tokens = getattr(usage, "output_tokens", 0)
            cache_read_tokens = getattr(usage, "cache_read_input_tokens", 0)
            cache_write_tokens = getattr(usage, "cache_creation_input_tokens", 0)
            return msg.content[0].text
        else:
            msg = client.chat.completions.create(
                model=model,
                max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system + system_suffix},
                    {"role": "user", "content": prompt},
                ],
            )
            usage = getattr(msg, "usage", None)
            input_tokens = getattr(usage, "prompt_tokens", 0)
            output_tokens = getattr(usage, "completion_tokens", 0)
            return msg.choices[0].message.content
    except Exception as error:
        failed = True
        _report_llm_error(error)
        raise
    finally:
        telemetry.record_llm(
            LLM_PROVIDER, perf_counter() - started, failed, input_tokens, output_tokens,
            cache_read_tokens, cache_write_tokens,
        )

# Keep in sync with handlers.INTENT_ACTIONS (plus "silent", which bot.py handles).
VALID_ACTIONS = {
    "add_book",
    "add_to_wishlist",
    "remove_from_wishlist",
    "queue_add",
    "queue_remove",
    "queue_list",
    "export_shelf",
    "start_book",
    "update_progress",
    "finish_book",
    "rate_book",
    "query",
    "group_query",
    "recommend",
    "summarize",
    "open_nominations",
    "nominate",
    "close_nominations",
    "list_nominations",
    "open_poll",
    "close_poll",
    "poll_results",
    "open_thread",
    "open_coordination_poll",
    "close_coordination_poll",
    "coordination_poll_results",
    "dnf_book",
    "year_in_review",
    "leaderboard",
    "club_history",
    "scrapbook",
    "scrapbook_add",
    "scrapbook_remove",
    "set_goal",
    "check_goal",
    "admin_dashboard",
    "set_reminder",
    "streak",
    "streak_leaderboard",
    "set_tone",
    "clear_tone",
    "list_tones",
    "manage_membership",
    "help",
    "current_read",
    "manage_club_read",
    "silent",
    "unknown",
}

SYSTEM_PROMPT = """You are a book club assistant. You help users track their reading, log progress, rate books, and get recommendations.

You only help with book club topics. If a user asks about something unrelated, redirect them warmly back to books.

All output must be in English only.
If the user's message is not in English, or they ask you to reply in another language, do not comply with the requested language.
Instead, return action "unknown", empty args, a short English-only message asking them to resend their request in English, and flag_injection false.

You must always respond with a single JSON object in this exact format:
{
    "actions": [
        {
            "action": "add_book | add_to_wishlist | remove_from_wishlist | queue_add | queue_remove | queue_list | export_shelf | start_book | update_progress | finish_book | dnf_book | rate_book | query | group_query | recommend | summarize | year_in_review | leaderboard | club_history | scrapbook | scrapbook_add | scrapbook_remove | set_goal | check_goal | admin_dashboard | set_reminder | streak | streak_leaderboard | set_tone | clear_tone | list_tones | manage_membership | help | current_read | manage_club_read | open_nominations | nominate | close_nominations | list_nominations | open_poll | close_poll | poll_results | open_thread | open_coordination_poll | close_coordination_poll | coordination_poll_results | silent | unknown",
            "args": {},
            "response": "Conversational response string for Discord"
        }
    ],
    "flag_injection": false
}

Multiple requests in one message:
- Most messages are one request: return exactly one item in "actions".
- If the user clearly asks for several separate things in one message (e.g. "I finished Dune, rate it 9/10, and I'm starting Hyperion"), return one item per request, in the order the user said them, up to 3 items.
- Two books for the same action are two items (e.g. "add Dune and Hyperion to my wishlist" → two add_to_wishlist items).
- Only include requests the user explicitly asked for. Never add follow-ups they did not ask for, and never split one request into several (a reason or comment attached to a request is part of that request).
- Each item's "response" covers only that item, kept to one or two short sentences. Python sends them separately, so do not repeat yourself across items.
- If user_context includes "focus_request", return exactly one item: the request matching its "action" and "args" (for repeated actions, "ordinal" is which of them, counting from 0). Ignore the other requests in the message.

Use "recommend" when the user asks for book recommendations, what to read next, or suggestions based on their taste. Optional args: {"similar_to": "book title", "genre": "genre name", "mood": "mood or vibe"} if they specify preferences.
Use "queue_add" when the user wants to save a book to their private reading queue / private TBR (not the public wishlist). args: {"title": "...", "author": "..." (optional)}.
Use "queue_remove" when the user wants to remove a book from their private queue. args: {"title": "...", "author": "..." (optional)}.
Use "queue_list" when the user asks to see their private queue / private TBR. Do not list book titles in "response" — Python reveals the queue ephemerally. Keep response to a short ack.
Use "export_shelf" when the user wants to export, download, or back up their shelf or reading history as a file/CSV, or move it to Goodreads, StoryGraph, or Hardcover. Empty args. Never list book titles in "response" — Python points them to the private /export command. Keep response to a short ack.
Use "add_to_wishlist" when the user wants the public wishlist / want-to-read shelf (visible if others ask about their shelf). args: {"title": "...", "author": "..." (optional)}.
Use "remove_from_wishlist" when the user wants to remove a book from their public wishlist. args: {"title": "...", "author": "..." (optional)}.
Use "dnf_book" when the user says they gave up on, didn't finish, abandoned, or are quitting a book. args: {"title": "...", "author": "..." (optional)}
Use "year_in_review" when the user asks for a year in review, annual recap, or how the year went for the book club.
Use "leaderboard" when the user asks for rankings, leaderboards, who read the most, stats, or standings.
Use "club_history" when the user asks what the club has read, finished books, the club archive, or reading history.
Use "scrapbook" to browse saved quotes, highlights, reactions, rating notes, or discussion memories for a club read. Optional args: {"title": "...", "session_id": N, "operation": "sessions"}. Use operation sessions to list past scrapbooks. With no book/session, Python selects the current or most recent club read. Keep response a short acknowledgement; Python reveals content privately because it can contain spoilers.
Use "scrapbook_add" only when explicitly asked to save something to the scrapbook. args: {"content": "exact user-provided text", "kind": "quote | highlight | reaction | rating_note | discussion", "title": "..." (optional), "session_id": N (optional)}. Default kind highlight. Never invent quotes, infer text to save, or copy other messages. Text must be 1-1000 characters. These notes are shared with enrolled readers in the server, not private personal notes. Do not repeat the content in response.
Use "scrapbook_remove" to remove the sender's own scrapbook entry. args: {"entry_id": N}. Ask for the entry number when absent; never guess an ID. Do not repeat scrapbook content in response.
Use "set_goal" when the user wants to set a reading goal or challenge for the year. args: {"goal": N} where N is the number of books.
Use "check_goal" when the user asks about their reading goal, how they're tracking, or their progress toward a target.
Use "admin_dashboard" when the user (admin) asks for a dashboard, club overview, or status summary of the whole group.
Use "streak" when the user asks about their reading streak, how many days in a row they've logged, or their reading consistency.
Use "streak_leaderboard" when the user asks for streak rankings, who has the longest streak, or reading consistency leaderboard.
Use "set_tone" when an admin wants to set a tone override for a user. args: {"user": "display name", "mode": "glaze | roast | neutral"}.
Use "clear_tone" when an admin wants to clear a user's tone override. args: {"user": "display name"}.
Use "list_tones" when an admin wants to see all active tone overrides.
Use "set_reminder" when the user wants to set a reminder for something related to their reading, like a check-in, deadline, or notification. args: {"message": "...", "book_title": "..." (optional), "scheduled_time": "..." (ISO timestamp or relative like 'in 2 hours', 'tomorrow at 3pm'), "recurrence": "once | daily | weekly | monthly"} (default "once").
Use "manage_membership" when the user wants to join or leave the book club, change their participation tier, enable or disable reading-nudge DMs, or check those settings. args: {"operation": "join | opt_out | show | enable_nudges | disable_nudges", "tier": "chat | tracking | updates | club"}. Tiers are cumulative: Tracking enables personal book tracking; Updates adds weekly recap and club-news pings on top of Tracking; Club adds nominations and polls on top of Updates; Chat keeps basic server access without book tracking. "tier" is required only when operation is "join" and defaults to "tracking" when omitted. Reading-nudge DMs are independent of tiers but require Tracking. Never include a target user: these settings always apply to the sender.
Use "help" when the user asks what the bot can do, asks for help, or asks how to use a feature. Optional args: {"topic": "books | club | polls | stats | membership | admin"}. Choose the closest topic when they ask about a specific feature; otherwise leave topic empty.
Use "silent" only when no reply is appropriate; still provide a brief response string to satisfy the JSON schema.
If user_context includes reading_goal, naturally reference their progress when relevant (e.g. finishing a book, checking shelf).
Use "summarize" when the user asks for a weekly recap, reading summary, or group activity update.
Use "query" when the user asks about their own shelf (currently reading, finished, wishlist) or a specific person's shelf.
Use "current_read" when the user asks what the CLUB is reading, the current club book, the shared session, or "what are we reading?" as a group pick. Do not list personal shelves here — Python posts the current-read embed. Keep response to a short ack.
Use "group_query" when the user asks what EVERYONE is reading on their personal shelves, "show everyone's shelf", "what is each person reading", etc. This is not the canonical club book.
Use "manage_club_read" when an admin wants to start, replace, complete, or set a target date for the club's current book. args: {"operation": "start | replace | complete | set_target", "title": "..." (start/replace), "author": "..." (optional), "target_date": "YYYY-MM-DD or clear" (optional on start; required for set_target)}. "replace" completes the current club book and starts a new one. Member progress on personal shelves is not changed by complete.
If user_context includes a nomination_reason for the book being rated, acknowledge that context naturally when responding to the rating.

Nomination actions:
- "open_nominations" — user wants to open a nomination window for the next book club pick. Optional args: {"days": N} if they specify a duration.
- "nominate" — user is nominating a book for the club to read. args: {"title": "...", "author": "..." (optional), "reason": "..."}. If the user is explicitly asking to change/replace an existing nomination (e.g. "replace my nomination with X", "change my nomination to X", "swap my pick for X"), also set "replace": true in args.
- "close_nominations" — user (admin) wants to close the nomination window early.
- "list_nominations" — user wants to see the current list of nominations.

Poll/voting actions:
- "open_poll" — admin wants to open voting on the nominated books. Optional args: {"days": N} if they specify a duration.
- "close_poll" — admin wants to close the poll early.
- "poll_results" — user wants to see current vote standings.

Thread actions:
- "open_thread" — user wants a spoiler-safe discussion thread for a book they have finished. args: {"title": "...", "author": "..." (optional), "topic": "..." (optional)}

Coordination poll actions (pace/date/scheduling votes):
- "open_coordination_poll" — admin wants to poll the group on pace, meeting dates, or any scheduling question. args: {"question": "...", "options": ["...", "...", ...], "days": N (optional)}
- "close_coordination_poll" — admin wants to close the coordination poll early.
- "coordination_poll_results" — user wants to see current standings on the coordination poll.

Never include any text outside the JSON object. No preamble, no explanation, just the JSON."""

TONE_INSTRUCTIONS = {
    "glaze": """

Tone override:
- The user has a temporary "glaze" mode enabled.
- Keep the response more enthusiastic and flattering than usual.
- Stay grounded in the user's actual reading or book-club activity.
- Do not invent achievements or use manipulative emotional language.
- Keep it playful and book-club specific, not intense or sycophantic.
""",
    "roast": """

Tone override:
- The user has a temporary "roast" mode enabled.
- Keep the response to light, playful book-club banter only.
- Tease reading pace, indecision, towering TBRs, or genre obsessions only when relevant.
- Never insult intelligence, appearance, identity, mental health, trauma, or real-life relationships.
- Never encourage dogpiling, humiliation, exclusion, or harassment.
- If the request is serious or sensitive, ignore roast mode and respond neutrally and helpfully.
""",
}


def tone_instructions(user_context: dict) -> str:
    """Per-user tone suffix, sent after SYSTEM_PROMPT's cache breakpoint."""
    tone = (user_context or {}).get("tone_override")
    if not isinstance(tone, str):
        tone = ""
    return TONE_INSTRUCTIONS.get(tone, "")


def fallback_intent(response: str = "Sorry, I had trouble understanding that. Could you rephrase?") -> dict:
    return {
        "action": "unknown",
        "args": {},
        "response": response,
        "flag_injection": False
    }


ENGLISH_ONLY_MESSAGE = "I can only help in English right now. Please resend that in English."

NON_LATIN_RANGES = (
    "\u0400-\u04FF"  # Cyrillic
    "\u0370-\u03FF"  # Greek
    "\u0590-\u05FF"  # Hebrew
    "\u0600-\u06FF"  # Arabic
    "\u0750-\u077F"
    "\u08A0-\u08FF"
    "\u0900-\u097F"  # Devanagari
    "\u0E00-\u0E7F"  # Thai
    "\u3040-\u309F"  # Hiragana
    "\u30A0-\u30FF"  # Katakana
    "\u4E00-\u9FFF"  # CJK Unified Ideographs
    "\uAC00-\uD7AF"  # Hangul
)

NON_ENGLISH_MARKERS = {
    "hola", "gracias", "por", "para", "libro", "leer", "quiero", "recomiendame",
    "bonjour", "merci", "livre", "lire", "salut", "voudrais",
    "hallo", "danke", "buch", "lesen", "bitte",
    "ciao", "grazie", "libro", "leggere", "voglio",
    "ola", "obrigado", "livro", "quero", "ler",
}


def english_only_intent(response: str = ENGLISH_ONLY_MESSAGE) -> dict:
    return {
        "action": "unknown",
        "args": {},
        "response": response,
        "flag_injection": False
    }


def _contains_non_latin_script(text: str) -> bool:
    return bool(re.search(f"[{NON_LATIN_RANGES}]", text))


def _looks_non_english_latin_text(text: str) -> bool:
    tokens = re.findall(r"[a-zA-ZÀ-ÿ']+", text.lower())
    if not tokens:
        return False

    marker_hits = sum(token in NON_ENGLISH_MARKERS for token in tokens)
    accented_chars = len(re.findall(r"[À-ÿ]", text))

    if marker_hits >= 2:
        return True

    return accented_chars >= 3 and marker_hits >= 1


def should_enforce_english_only(text: str) -> bool:
    if not isinstance(text, str):
        return False

    normalized = text.strip()
    if not normalized:
        return False

    if _contains_non_latin_script(normalized):
        return True

    return _looks_non_english_latin_text(normalized)


MAX_ACTIONS = 3
# Room for MAX_ACTIONS items with args and replies; billing is on tokens actually written.
INTENT_MAX_TOKENS = 2048


def _validate_action_item(item, flag_injection: bool = False) -> dict | None:
    """One {action, args, response} item, normalized to today's intent shape; None if invalid."""
    if not isinstance(item, dict):
        return None
    action = item.get("action")
    args = item.get("args")
    response = item.get("response")
    if action not in VALID_ACTIONS:
        return None
    if not isinstance(args, dict):
        return None
    if not isinstance(response, str) or not response.strip():
        return None
    return {
        "action": action,
        "args": args,
        "response": response,
        "flag_injection": flag_injection,
    }


def validate_intent(intent: dict) -> dict:
    if not isinstance(intent, dict) or not isinstance(intent.get("flag_injection"), bool):
        return fallback_intent()
    return _validate_action_item(intent, intent["flag_injection"]) or fallback_intent()


def validate_intents(payload) -> list[dict]:
    """Normalize an LLM payload into 1..MAX_ACTIONS intents.

    Accepts the multi-action shape ({"actions": [...], "flag_injection": ...}) and
    the legacy single-object shape. Invalid items are dropped and exact duplicates
    collapsed; if nothing valid remains the result is a single fallback intent.
    flag_injection is message-wide, so it is copied onto every returned intent.
    """
    if not isinstance(payload, dict):
        return [fallback_intent()]
    flag_injection = payload.get("flag_injection")
    if not isinstance(flag_injection, bool):
        return [fallback_intent()]
    items = payload.get("actions")
    if items is None and "action" in payload:
        items = [payload]
    if not isinstance(items, list):
        return [fallback_intent()]

    intents, seen = [], set()
    for item in items:
        intent = _validate_action_item(item, flag_injection)
        if intent is None:
            continue
        key = json.dumps([intent["action"], intent["args"]], sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        intents.append(intent)
        if len(intents) == MAX_ACTIONS:
            break
    return intents or [fallback_intent()]


def get_intents(user_message: str, user_context: dict) -> list[dict]:
    """
    Takes a user message and context, calls Claude once, returns 1..MAX_ACTIONS intents
    in the order the user asked for them. All LLM logic lives here.
    """
    if not SINGLE_CALL_MODE:
        # Currently the system is built for single-call mode only.
        pass

    if should_enforce_english_only(user_message):
        return [english_only_intent()]

    prompt = f"""User message: {user_message}

User context: {json.dumps(user_context)}"""

    try:
        raw = _call_llm(
            SYSTEM_PROMPT, prompt, INTENT_MAX_TOKENS,
            system_suffix=tone_instructions(user_context), cache_system=True,
        )
    except Exception:
        return [fallback_intent(
            "Sorry, I'm having trouble reaching the book brain right now. Try again in a minute."
        )]

    return validate_intents(_parse_json_payload(raw))


def focus_request(planned: list[dict], index: int) -> dict:
    """Identify planned[index] for a re-intent pass over a multi-request message.

    The action name alone is ambiguous when one message repeats an action
    ("rate Dune 9 and Hyperion 7"), so carry the args and the item's position
    among planned items of the same action.
    """
    intent = planned[index]
    action = intent.get("action")
    ordinal = sum(1 for earlier in planned[:index] if earlier.get("action") == action)
    return {"action": action, "args": intent.get("args", {}), "ordinal": ordinal}


def _norm_title(args) -> str:
    title = args.get("title") if isinstance(args, dict) else None
    return " ".join(title.casefold().split()) if isinstance(title, str) else ""


def _pick_focused(intents: list[dict], focus: dict) -> dict | None:
    candidates = [intent for intent in intents if intent["action"] == focus.get("action")]
    if len(candidates) <= 1:
        return candidates[0] if candidates else None
    args = focus.get("args") or {}
    for intent in candidates:
        if intent["args"] == args:
            return intent
    title = _norm_title(args)
    if title:
        for intent in candidates:
            if _norm_title(intent["args"]) == title:
                return intent
    ordinal = focus.get("ordinal")
    if isinstance(ordinal, int) and 0 <= ordinal < len(candidates):
        return candidates[ordinal]
    return candidates[0]


def get_intent(user_message: str, user_context: dict) -> dict:
    """Single-intent view of get_intents, used by handler re-intent passes.

    When user_context["focus_request"] is set (a multi-request message being
    dispatched one action at a time), return the item for that exact request.
    """
    intents = get_intents(user_message, user_context)
    focus = (user_context or {}).get("focus_request")
    if isinstance(focus, dict):
        picked = _pick_focused(intents, focus)
        if picked is not None:
            return picked
    return intents[0]


def _parse_json_payload(raw: str):
    clean = raw.strip()
    if clean.startswith("```"):
        clean = clean.split("\n", 1)[1]  # remove opening fence line
        clean = clean.rsplit("```", 1)[0]  # remove closing fence
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        return None


def get_recap(activity: dict) -> str:
    """
    Takes weekly activity data and returns a conversational recap string for Discord.
    """
    prompt = f"""Here is the book club's reading activity from the last 7 days:

{json.dumps(activity, indent=2)}

Write a warm, conversational weekly recap for the group. Mention who started or finished books, any ratings logged, and give a sense of what everyone is reading. Keep it friendly and encouraging. If there was no activity, say so warmly. Plain text only, no JSON."""

    return _call_llm(
        "You are a book club assistant writing a weekly reading recap for a small friend group. Be warm, conversational, and encouraging. Respond in English only. Plain text only.",
        prompt,
        1024,
    )


def get_weekly_post(activity: dict) -> str:
    """Combined weekly recap + check-in in a single concise message."""
    prompt = f"""Here is the book club's reading activity from the last 7 days:

{json.dumps(activity, indent=2)}

Write ONE short, warm Discord message that combines a brief recap of recent activity with a natural check-in asking what everyone is reading. Keep it to 3-5 sentences total. Be specific about who did what, but don't list every detail. Plain text only."""

    return _call_llm(
        "You are a book club assistant writing a combined weekly recap + check-in for a small friend group. Be warm, conversational, specific, and very concise. Respond in English only. Plain text only.",
        prompt,
        512,
    )


def _strip_json_fence(raw: str) -> str:
    clean = raw.strip()
    if clean.startswith("```"):
        clean = clean.split("\n", 1)[1]
        clean = clean.rsplit("```", 1)[0]
    return clean.strip()


def parse_recommendation_payload(raw: str) -> dict:
    """Parse structured recommendation JSON; fall back to plain-text intro only."""
    if not isinstance(raw, str) or not raw.strip():
        return {"intro": "", "books": []}
    try:
        data = json.loads(_strip_json_fence(raw))
    except json.JSONDecodeError:
        return {"intro": raw.strip(), "books": []}
    if not isinstance(data, dict):
        return {"intro": raw.strip(), "books": []}
    intro = data.get("intro")
    if not isinstance(intro, str):
        intro = ""
    books = []
    for item in data.get("books") or []:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        if not isinstance(title, str) or not title.strip():
            continue
        author = item.get("author")
        why = item.get("why")
        books.append(
            {
                "title": title.strip(),
                "author": author.strip() if isinstance(author, str) and author.strip() else None,
                "why": why.strip() if isinstance(why, str) and why.strip() else None,
            }
        )
    return {"intro": intro.strip(), "books": books[:5]}


def format_recommendation_text(payload: dict) -> str:
    """Render structured recommendations into Discord-friendly plain text."""
    intro = (payload.get("intro") or "").strip()
    books = payload.get("books") or []
    if not books:
        return intro
    lines = []
    if intro:
        lines.append(intro)
        lines.append("")
    for book in books:
        author = f" by {book['author']}" if book.get("author") else ""
        why = f" — {book['why']}" if book.get("why") else ""
        lines.append(f"• **{book['title']}**{author}{why}")
    return "\n".join(lines).strip()


def get_recommendation(user_message: str, args: dict, user_history: dict) -> dict:
    """
    Takes a user's reading history and request, returns structured recommendations.

    Shape: {"intro": str, "books": [{"title", "author?", "why?"}]}
    On parse failure, books is empty and intro holds the raw text.
    """
    prompt = f"""The user wants book recommendations.

Their request: {user_message}
Specific preferences from their request: {json.dumps(args)}

Their reading history:
- Finished books (with ratings out of 10): {json.dumps(user_history.get("finished", []))}
- Currently reading: {json.dumps(user_history.get("reading", []))}
- Wishlist: {json.dumps(user_history.get("wishlist", []))}

Recommend 3-5 books. Use their ratings to infer taste — high scores mean they loved it. Avoid books already in their history. Be specific about why each book suits them.

Respond with a single JSON object only, no markdown fences:
{{"intro": "short warm lead-in", "books": [{{"title": "...", "author": "...", "why": "one short sentence"}}]}}"""

    try:
        raw = _call_llm(
            "You are a book club assistant with deep knowledge of literature across all genres. Give warm, specific book recommendations. Respond in English only. Return JSON only.",
            prompt,
            1024,
        )
    except Exception:
        return {"intro": "I couldn't fetch recommendations right now. Try again in a minute.", "books": []}
    return parse_recommendation_payload(raw)


def get_dnf_eulogy(title: str | None, author: str | None, reading_history: list) -> str:
    """
    Generates a short mock eulogy for a book the user abandoned.
    """
    author_str = f" by {author}" if author else ""
    prompt = f"""The user just gave up on "{title}"{author_str}.

Their reading history: {json.dumps(reading_history)}

Write a short, warm, and gently funny mock eulogy for the abandoned book — as if mourning a relationship that just wasn't meant to be. 2-4 sentences. Acknowledge what drew them to it, and wish the book well with the next reader. Plain text only."""

    return _call_llm(
        "You are a book club assistant with a warm sense of humor. Write a gentle, funny eulogy for an abandoned book. Respond in English only. Plain text only.",
        prompt,
        256,
    )


def get_leaderboard_narrative(stats: list) -> str:
    """
    Takes per-user reading stats and returns a fun leaderboard post for Discord.
    """
    prompt = f"""Here are the book club reading stats for all members:

{json.dumps(stats, indent=2)}

Write a fun, warm leaderboard post for the group. Celebrate the top readers, give shoutouts for interesting stats (most pages, highest average rating, most DNFs, etc.). Keep it friendly and encouraging — no trash talk. Plain text only."""

    return _call_llm(
        "You are a book club assistant writing a fun reading leaderboard post. Celebrate achievements, give shoutouts, keep it warm and competitive in a friendly way. Respond in English only. Plain text only.",
        prompt,
        1024,
    )

def get_club_history_narrative(history: list) -> str:
    """
    Takes finished-book rows and returns a warm archive post for Discord.
    """
    prompt = f"""Here are the books our book club has finished, most recent first:

{json.dumps(history, indent=2)}

Write a warm, brief tour of our reading history. Name a few standout titles, note highly rated ones and prolific finishers. Keep it nostalgic and inviting — no trash talk. Plain text only."""

    return _call_llm(
        "You are a book club assistant writing a warm tour of the club's finished books for a small friend group. Be specific, nostalgic, and inviting. Respond in English only. Plain text only.",
        prompt,
        1024,
    )


def get_year_in_review(activity: dict) -> str:
    """
    Takes full-year activity data and returns a celebratory year-end narrative.
    """
    prompt = f"""Here is the book club's full reading activity for the year:

{json.dumps(activity, indent=2)}

Write a warm, celebratory year-in-review for the group. Highlight standout readers, favorite books by rating, any DNF moments worth noting, and achievements earned. Give the year a personality — was it a slow burn year? A prolific year? A year of hidden gems? Keep it fun, personal, and specific. Plain text only."""

    return _call_llm(
        "You are a book club assistant writing a fun, celebratory year-end reading retrospective for a small friend group. Be warm, specific, and give the year a narrative arc. Respond in English only. Plain text only.",
        prompt,
        1500,
    )


def get_taste_profile(finished_books: list) -> str:
    """
    Generates a 2-3 sentence reader taste profile from the user's finished books and ratings.
    """
    prompt = f"""Based on this reader's finished books and ratings (out of 10):

{json.dumps(finished_books, indent=2)}

Write a 2-3 sentence taste profile describing their reading personality — what they love, what they tend to avoid, any patterns in their ratings. Be specific and insightful. Plain text only."""

    return _call_llm(
        "You are a literary analyst writing a concise reader taste profile. Be specific, insightful, and warm. Respond in English only. Plain text only.",
        prompt,
        256,
    )


def get_checkin_prompt(activity: dict) -> str:
    """
    Takes recent activity data and returns a short check-in prompt for Discord.
    """
    prompt = f"""Here is the book club's recent reading activity:

{json.dumps(activity, indent=2)}

Write a short, warm Discord check-in prompt that asks members what they are reading right now and whether they have made progress. Keep it to 2-4 sentences, plain text only, and avoid sounding repetitive."""

    return _call_llm(
        "You are a book club assistant writing a periodic check-in prompt for a small friend group. Be warm, conversational, and concise. Respond in English only. Plain text only.",
        prompt,
        256,
    )
