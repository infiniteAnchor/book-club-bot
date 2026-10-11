import os
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

ENV = os.getenv("ENV", "prod")  # set ENV=dev locally for DISCORD_TOKEN_DEV and intent debug output
# Dev falls back to DISCORD_TOKEN so a single-token setup still starts.
DISCORD_TOKEN = (
    (os.getenv("DISCORD_TOKEN_DEV") or os.getenv("DISCORD_TOKEN"))
    if ENV == "dev"
    else os.getenv("DISCORD_TOKEN")
)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
GOOGLE_BOOKS_API_KEY = os.getenv("GOOGLE_BOOKS_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

# Every configured credential, so logs and fallbacks can scrub exact values.
SECRET_VALUES = tuple(
    value
    for value in (
        os.getenv("DISCORD_TOKEN"),
        os.getenv("DISCORD_TOKEN_DEV"),
        ANTHROPIC_API_KEY,
        GOOGLE_BOOKS_API_KEY,
        OPENAI_API_KEY,
        DEEPSEEK_API_KEY,
    )
    if value
)

# LLM provider: "anthropic" | "openai" | "deepseek"
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "anthropic")
LLM_MODEL = os.getenv("LLM_MODEL", "")  # empty = sensible default per provider
DEFAULT_LLM_MODELS = {
    "anthropic": "claude-sonnet-4-6",
    "openai": "gpt-4o",
    "deepseek": "deepseek-chat",
}
ADMIN_DISCORD_ID = os.getenv("ADMIN_DISCORD_ID")

def _default_app_version() -> str:
    """Newest CHANGELOG heading; ``0.1.0`` was the pre-date-heading placeholder."""
    from release_notes import latest_changelog_version
    return latest_changelog_version()


# Central application version. Override with APP_VERSION for deploys. The old
# placeholder ``0.1.0`` is treated as unset so /status and announcements follow
# CHANGELOG.md dates unless a real override is set.
_env_app_version = os.getenv("APP_VERSION", "").strip()
APP_VERSION = (
    _env_app_version
    if _env_app_version and _env_app_version != "0.1.0"
    else _default_app_version()
)
BOT_START_TIME = datetime.now(timezone.utc)
LLM_ALERT_COOLDOWN_MINUTES = int(os.getenv("LLM_ALERT_COOLDOWN_MINUTES", "720"))


def resolve_llm_model(provider=None, configured_model=None):
    """Return the configured model or the shared default for a provider."""
    provider = provider if provider is not None else LLM_PROVIDER
    configured_model = configured_model if configured_model is not None else LLM_MODEL
    return configured_model or DEFAULT_LLM_MODELS.get(provider, "")

# Process-level flags (not per-guild)
SINGLE_CALL_MODE = True
CONVERSATIONAL_RESPONSES = True
DEBUG_INTENT = os.getenv("DEBUG_INTENT", str(ENV != "prod")).lower() in ("1", "true", "yes")  # default on in dev, off in prod; override via env
# Master switch for member-facing release notes. Per-guild settings can opt out
# when this is on, but can never override a global pause.
RELEASE_ANNOUNCEMENTS_ENABLED = os.getenv(
    "RELEASE_ANNOUNCEMENTS_ENABLED", "false"
).lower() in ("1", "true", "yes")

# Club-behavior defaults for NEW guilds only. Runtime values live in the
# guild_settings table (see ensure_guild_settings). Changing these does not
# rewrite existing rows; add a data migration if existing guilds should change.
WEEKLY_RECAP_SCHEDULED = True
COMBINED_WEEKLY_POST = True
TASTE_PROFILE_SCHEDULED = True
INACTIVITY_NUDGE_SCHEDULED = True
MEMBER_OPT_IN_ENABLED = True
RELEASE_ANNOUNCEMENTS_CHANNEL = os.getenv("RELEASE_ANNOUNCEMENTS_CHANNEL", "book-bot")
YEAR_IN_REVIEW_CHANNEL = "book-bot"
LEADERBOARD_CHANNEL = "book-bot"
WEEKLY_RECAP_CHANNEL = "book-bot"
CHECKIN_PROMPT_SCHEDULED = True
CHECKIN_PROMPT_CHANNEL = "book-bot"
LISTEN_CHANNEL_ONLY = False
LISTEN_CHANNEL_NAME = ""
REQUIRE_MENTION = True
REPLY_CHANNEL_NAME = ""  # empty = reply in same channel; set to e.g. "book-bot" to redirect

NOMINATIONS_ADMIN_ROLE = "Book Club Admin"
NOMINATIONS_DEFAULT_DAYS = 7
NOMINATIONS_CHANNEL = "book-bot"
OPT_IN_WELCOME_CHANNEL = os.getenv("OPT_IN_WELCOME_CHANNEL", "book-bot")

POLL_DEFAULT_DAYS = 7
COORDINATION_POLL_DEFAULT_DAYS = 3
INACTIVITY_NUDGE_DAYS = 14
INACTIVITY_NUDGE_INTERVAL_HOURS = 336  # biweekly (14 days)
CHECKIN_PROMPT_DAY_OF_WEEK = "sat"
CHECKIN_PROMPT_HOUR = 9
WEEKLY_RECAP_DAY_OF_WEEK = "sat"
WEEKLY_RECAP_HOUR = 9
THREAD_AUTO_ARCHIVE_MINUTES = 1440
POLL_THREAD_RETENTION_DAYS = 7
