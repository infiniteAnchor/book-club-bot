"""Operational-health helpers for /status.

Pure formatting stays here so it is testable without Discord. Discord I/O
lives in commands.py (ephemeral slash command). Version is read from
config.APP_VERSION — the single source of truth.
"""
from __future__ import annotations

import math
import os
import platform
from datetime import datetime, timezone

import discord

import database
from config import APP_VERSION, BOT_START_TIME, LLM_MODEL, LLM_PROVIDER, resolve_llm_model
from embeds import make_embed



def format_uptime(delta_seconds: float | int | None) -> str:
    """Turn a duration in seconds into a compact human string.

    Returns "Unavailable" for None/invalid input so the caller never has to
    branch on secrets or exceptions.
    """
    if delta_seconds is None:
        return "Unavailable"
    try:
        total = int(delta_seconds)
    except (TypeError, ValueError):
        return "Unavailable"
    if total < 0:
        total = 0
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def format_db_size(num_bytes: int | None) -> str:
    """Format a byte count with one decimal, graceful on missing input."""
    if num_bytes is None:
        return "Unavailable"
    try:
        size = int(num_bytes)
    except (TypeError, ValueError):
        return "Unavailable"
    if size < 0:
        return "Unavailable"
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    if size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    return f"{size / (1024 * 1024 * 1024):.1f} GB"


def format_latency(latency_seconds: float | None) -> str:
    """Format Discord WebSocket latency."""
    if latency_seconds is None:
        return "Unavailable"
    try:
        value = float(latency_seconds)
    except (TypeError, ValueError):
        return "Unavailable"
    if math.isnan(value) or math.isinf(value) or value < 0:
        return "Unavailable"
    # discord.py reports nan before first heartbeat; treat as unavailable
    # Round to whole milliseconds and cap field length.
    ms = int(round(value * 1000))
    return f"{ms} ms"


def get_llm_display() -> str:
    """Return provider · model without ever exposing secrets."""
    provider = (LLM_PROVIDER or "unknown").strip() or "unknown"
    model = (LLM_MODEL or "").strip()
    if not model:
        model = resolve_llm_model(provider, model) or "unknown"
    # Keep value short for Discord field limits
    display = f"{provider} · {model}"
    return display[:100]


def get_python_runtime() -> str:
    """Return the running Python implementation and version, e.g. 'CPython 3.12.13'."""
    return format_python_runtime(platform.python_implementation() + " " + platform.python_version())


def format_python_runtime(raw: str | None) -> str:
    """Format a 'Implementation X.Y.Z' string, tolerating bad input."""
    if not raw or not raw.strip():
        return "Unavailable"
    text = raw.strip()
    # Collapse "CPython 3.12.13" style; keep short for Discord field limits
    parts = text.split()
    impl = parts[0] if parts else text
    version = next((p for p in parts[1:] if p and p[0].isdigit()), "")
    display = f"{impl} {version}".strip()
    return display[:100] if display else "Unavailable"


def get_db_size_str() -> str:
    """Return a formatted DB size or 'Unavailable' if the file is missing."""
    try:
        path = database.DB_PATH
        if not os.path.exists(path):
            return "Unavailable"
        size = os.path.getsize(path)
        return format_db_size(size)
    except OSError:
        return "Unavailable"


def get_uptime_str(*, now: datetime | None = None) -> str:
    """Return a formatted uptime string from BOT_START_TIME."""
    try:
        current = now or datetime.now(timezone.utc)
        start = BOT_START_TIME
        if start is None:
            return "Unavailable"
        # Ensure both are timezone-aware
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        delta = (current - start).total_seconds()
        return format_uptime(delta)
    except Exception:
        return "Unavailable"


def build_status_embed(
    *,
    uptime: str | None = None,
    version: str | None = None,
    db_size: str | None = None,
    latency: str | None = None,
    llm: str | None = None,
    python_runtime: str | None = None,
) -> discord.Embed:
    """Build the warm operational-health embed with Discord-safe lengths.

    All args are pre-formatted strings; None falls back to a live lookup so
    tests can inject edge cases while production uses real metrics.
    """
    uptime_val = uptime if uptime is not None else get_uptime_str()
    version_val = version if version is not None else (APP_VERSION or "unknown")
    db_size_val = db_size if db_size is not None else get_db_size_str()
    llm_val = llm if llm is not None else get_llm_display()
    runtime_val = python_runtime if python_runtime is not None else get_python_runtime()
    # latency is only known per-interaction; require caller to supply it
    latency_val = latency if latency is not None else "Unavailable"

    embed = make_embed(
        "💚 Book Club Bot — Status",
        "Operational health at a glance.",
        kind="info",
    )
    # Use the warm embed helpers so field lengths are capped at 1024/256.
    # Order mirrors the roadmap bullet list.
    embed.add_field(name="Uptime", value=uptime_val[:1024] or "—", inline=True)
    embed.add_field(name="Version", value=version_val[:1024] or "—", inline=True)
    embed.add_field(name="Database", value=db_size_val[:1024] or "—", inline=True)
    embed.add_field(name="Latency", value=latency_val[:1024] or "—", inline=True)
    embed.add_field(name="LLM", value=llm_val[:1024] or "—", inline=True)
    embed.add_field(name="Runtime", value=runtime_val[:1024] or "—", inline=True)
    return embed
