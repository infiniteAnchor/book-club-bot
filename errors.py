"""Centralized error handling: full-detail logging + Discord-safe fallbacks.

Every unexpected exception flows through here so that:
- Logs capture the full traceback plus operational context (surface, action,
  guild, user, command, job) for debugging.
- Users only ever see a short, static, friendly fallback message.
- Nothing secret (API keys, SQL, tracebacks, internal paths, provider response
  bodies) reaches Discord — the user-facing copy is a fixed constant and any
  dynamic text that could reach a channel goes through redact() first.

This module is intentionally dependency-light (stdlib + discord + config +
helpers) so every layer can import it without cycles.
"""
import logging
import re

import discord

from config import SECRET_VALUES
from helpers import get_reply_channel

logger = logging.getLogger("bookclub")

# Static by design: nothing dynamic is ever appended to this.
FRIENDLY_ERROR_MESSAGE = (
    "Something went wrong on my side while handling that. Try again in a minute."
)

_SECRET_TOKEN_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
# Discord bot tokens (base64 user ID . timestamp . HMAC) and Google API keys.
_DISCORD_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{27,}\b")
_GOOGLE_KEY_RE = re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|authorization|bearer|password|secret|token)\b\s*[:=]\s*\S+"
)
_TRACEBACK_HEADER_RE = re.compile(r"(?im)^traceback \(most recent call last\):$")
_WINDOWS_PATH_RE = re.compile(r"(?i)\b[A-Z]:\\(?:[\w.@+-]+\\)+[\w.@+-]+")
_UNIX_PATH_RE = re.compile(r"(?<![\w])/(?:[\w.@+-]+/)+[\w.@+-]+")


def redact_secrets(text) -> str:
    """Strip configured credentials and credential-shaped strings, nothing else."""
    if not isinstance(text, str):
        text = str(text)
    for key in SECRET_VALUES:
        text = text.replace(key, "[REDACTED]")
    text = _SECRET_ASSIGNMENT_RE.sub("[REDACTED]", text)
    text = _SECRET_TOKEN_RE.sub("[REDACTED]", text)
    text = _DISCORD_TOKEN_RE.sub("[REDACTED]", text)
    text = _GOOGLE_KEY_RE.sub("[REDACTED]", text)
    return text


def redact(text) -> str:
    """Strip secrets, tracebacks, and internal paths from text meant for output."""
    text = redact_secrets(text)
    text = _TRACEBACK_HEADER_RE.sub("[TRACEBACK REMOVED]", text)
    text = _WINDOWS_PATH_RE.sub("[PATH]", text)
    text = _UNIX_PATH_RE.sub("[PATH]", text)
    return text


class RedactingFormatter(logging.Formatter):
    """Formatter that scrubs secrets from the whole record, tracebacks included.

    Paths and tracebacks stay so host logs remain debuggable; only credentials go.
    """

    def format(self, record: logging.LogRecord) -> str:
        return redact_secrets(super().format(record))


def install_log_redaction() -> None:
    """Swap every root handler's formatter for a redacting one, keeping its format."""
    for handler in logging.getLogger().handlers:
        current = handler.formatter
        handler.setFormatter(RedactingFormatter(
            current._fmt if current else None,
            current.datefmt if current else None,
        ))


def log_exception(exc: BaseException, *, surface: str, guild_id=None, user=None,
                  action=None, command=None, job=None) -> None:
    """Log the full traceback plus operational context. Logs only — never Discord."""
    context = {"surface": surface}
    if guild_id is not None:
        context["guild"] = str(guild_id)
    if user is not None:
        context["user"] = str(user)
    if action is not None:
        context["action"] = str(action)
    if command is not None:
        context["command"] = str(command)
    if job is not None:
        context["job"] = str(job)
    parts = " ".join(f"{k}={v}" for k, v in context.items())
    logger.error(
        "Unhandled exception [%s]: %s",
        parts,
        redact(f"{type(exc).__name__}: {exc}"),
        exc_info=exc,
    )


async def handle_message_failure(message, exc: BaseException, *, action=None,
                                 guild_id=None, user=None) -> None:
    """Log an unexpected natural-language failure and send the safe fallback."""
    log_exception(exc, surface="message", guild_id=guild_id, user=user, action=action)
    try:
        channel = get_reply_channel(message)
    except Exception:
        logger.exception("Could not resolve a reply channel for the fallback message")
        return
    try:
        await channel.send(FRIENDLY_ERROR_MESSAGE)
    except Exception:
        logger.exception("Failed to deliver the fallback error message")


def _interaction_command_name(interaction) -> str | None:
    try:
        name = getattr(getattr(interaction, "command", None), "qualified_name", None)
        if name:
            return str(name)
        return str((getattr(interaction, "data", {}) or {}).get("name"))
    except Exception:
        return None


async def handle_interaction_failure(interaction, error: BaseException, *,
                                     command=None) -> None:
    """Log an unexpected interaction failure and answer it safely.

    Uses interaction.response when nothing was sent yet, or followup once the
    response has been consumed (e.g. after defer()). Never raises, so it is
    safe as a global on_error hook.
    """
    original = getattr(error, "original", error)

    if command is None:
        command = _interaction_command_name(interaction)
    user = None
    try:
        user = f"{interaction.user} ({interaction.user.id})"
    except Exception:
        pass
    guild_id = None
    try:
        guild_id = str(interaction.guild_id) if interaction.guild_id else None
    except Exception:
        pass

    # Expected authorization rejections keep their own messaging; log quietly.
    if isinstance(error, discord.app_commands.CheckFailure):
        logger.warning(
            "Interaction rejected [surface=interaction command=%s guild=%s user=%s]",
            command, guild_id, user,
        )
        return

    log_exception(original, surface="interaction", guild_id=guild_id,
                  user=user, command=command)

    try:
        if interaction.response.is_done():
            await interaction.followup.send(FRIENDLY_ERROR_MESSAGE, ephemeral=True)
        else:
            await interaction.response.send_message(FRIENDLY_ERROR_MESSAGE, ephemeral=True)
    except Exception:
        logger.exception("Failed to deliver the fallback interaction response")


class SafeUIMixin:
    """Shared View/Modal error hook routing unexpected failures centrally."""

    async def on_error(self, interaction: discord.Interaction, error: Exception, item=None) -> None:
        await handle_interaction_failure(interaction, error, command=type(self).__name__)
