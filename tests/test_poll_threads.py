"""Regression tests for the duplicate anchor-post guard in create_poll_thread."""

import asyncio
import os
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
import poll_threads


def _temp_db():
    tmpdir = tempfile.TemporaryDirectory()
    db_path = os.path.join(tmpdir.name, "bookclub.db")
    patcher = patch.object(database, "DB_PATH", db_path)
    patcher.start()
    database.init_db()
    return patcher, tmpdir


def _channel(message_id=111):
    chan = SimpleNamespace()
    chan.id = 42
    chan.guild = SimpleNamespace(id=1)
    chan.send = AsyncMock(return_value=SimpleNamespace(id=message_id, create_thread=AsyncMock()))
    return chan


def test_create_poll_thread_posts_anchor():
    """First call for a workflow posts the anchor and records the thread."""
    patcher, tmpdir = _temp_db()
    try:
        with patch.object(
            poll_threads, "get_guild_settings",
            lambda gid: {"thread_auto_archive_minutes": 60},
        ):
            channel = _channel()
            result = asyncio.run(
                poll_threads.create_poll_thread(
                    channel, "1", "nominations", 5, "🗳️ Nominations",
                    "🗳️ Nominations open", "Noms are open.",
                    SimpleNamespace(), None,
                )
            )
            assert result is not None
            assert channel.send.await_count == 1
    finally:
        patcher.stop()
        tmpdir.cleanup()


def test_create_poll_thread_skips_duplicate_anchor():
    """Second call for the same workflow must NOT post another anchor."""
    patcher, tmpdir = _temp_db()
    try:
        with patch.object(
            poll_threads, "get_guild_settings",
            lambda gid: {"thread_auto_archive_minutes": 60},
        ):
            channel = _channel()

            async def run_twice():
                first = await poll_threads.create_poll_thread(
                    channel, "1", "nominations", 5, "🗳️ Nominations",
                    "🗳️ Nominations open", "Noms are open.",
                    SimpleNamespace(), None,
                )
                second = await poll_threads.create_poll_thread(
                    channel, "1", "nominations", 5, "🗳️ Nominations",
                    "🗳️ Nominations open", "Noms are open.",
                    SimpleNamespace(), None,
                )
                return first, second

            first, second = asyncio.run(run_twice())
            assert first is not None
            # Second call is a no-op: no new anchor posted.
            assert isinstance(second, dict)
            assert channel.send.await_count == 1
    finally:
        patcher.stop()
        tmpdir.cleanup()
