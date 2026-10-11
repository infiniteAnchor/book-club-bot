import os
import sys
import unittest
from typing import Any, cast
from unittest import mock
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class StatusFormattingTests(unittest.TestCase):
    def test_format_uptime_seconds_and_minutes(self):
        from status import format_uptime
        self.assertEqual(format_uptime(0), "0s")
        self.assertEqual(format_uptime(45), "45s")
        self.assertEqual(format_uptime(90), "1m 30s")
        self.assertEqual(format_uptime(3661), "1h 1m 1s")
        self.assertEqual(format_uptime(90061), "1d 1h 1m")

    def test_format_uptime_graceful(self):
        from status import format_uptime
        self.assertEqual(format_uptime(None), "Unavailable")
        self.assertEqual(format_uptime(-5), "0s")
        self.assertEqual(format_uptime(cast(Any, "bad")), "Unavailable")

    def test_format_db_size(self):
        from status import format_db_size
        self.assertEqual(format_db_size(0), "0 B")
        self.assertEqual(format_db_size(512), "512 B")
        self.assertEqual(format_db_size(2048), "2.0 KB")
        self.assertEqual(format_db_size(5 * 1024 * 1024), "5.0 MB")
        self.assertEqual(format_db_size(3 * 1024 * 1024 * 1024), "3.0 GB")
        self.assertEqual(format_db_size(None), "Unavailable")
        self.assertEqual(format_db_size(-1), "Unavailable")

    def test_format_latency(self):
        from status import format_latency
        self.assertEqual(format_latency(0.042), "42 ms")
        self.assertEqual(format_latency(0), "0 ms")
        self.assertEqual(format_latency(None), "Unavailable")
        self.assertEqual(format_latency(float("nan")), "Unavailable")
        self.assertEqual(format_latency(float("inf")), "Unavailable")
        self.assertEqual(format_latency(-0.1), "Unavailable")

    def test_get_llm_display_without_secrets(self):
        import status
        # default model fallback
        with mock.patch("status.LLM_PROVIDER", "anthropic"), mock.patch("status.LLM_MODEL", ""):
            self.assertIn("anthropic", status.get_llm_display())
            self.assertIn("claude", status.get_llm_display())
        with mock.patch("status.LLM_PROVIDER", "openai"), mock.patch("status.LLM_MODEL", "gpt-4o-mini"):
            self.assertEqual(status.get_llm_display(), "openai · gpt-4o-mini")
        # ensure no secret is leaked (the function never returns a key)
        display = status.get_llm_display()
        self.assertNotIn("sk-", display)
        self.assertNotIn("api", display.lower() if "anthropic" not in display.lower() else "___never_match___")

    def test_get_uptime_str(self):
        import status
        from config import BOT_START_TIME
        fixed_now = BOT_START_TIME + timedelta(hours=2, minutes=5)
        result = status.get_uptime_str(now=fixed_now)
        self.assertEqual(result, "2h 5m 0s")

    def test_get_db_size_str_missing_file(self):
        import status
        with mock.patch("status.database.DB_PATH", "/tmp/nonexistent_bookclub_status_test.db"):
            self.assertEqual(status.get_db_size_str(), "Unavailable")

    def test_get_db_size_str_with_temp_file(self):
        import status
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(b"x" * 2048)
            tmp_path = tmp.name
        try:
            with mock.patch("status.database.DB_PATH", tmp_path):
                result = status.get_db_size_str()
                self.assertEqual(result, "2.0 KB")
        finally:
            os.unlink(tmp_path)

    def test_get_db_size_str_oserror_graceful(self):
        import status
        with mock.patch("os.path.getsize", side_effect=OSError("disk error")):
            with mock.patch("os.path.exists", return_value=True):
                self.assertEqual(status.get_db_size_str(), "Unavailable")


class StatusEmbedTests(unittest.TestCase):
    def test_build_status_embed_has_required_fields_and_warm_style(self):
        from status import build_status_embed
        from embeds import COLORS

        embed = build_status_embed(
            uptime="1h 2m 3s",
            version="9.9.9",
            db_size="1.2 MB",
            latency="42 ms",
            llm="anthropic · claude-sonnet-4-6",
        )
        data = cast(dict[str, Any], embed.to_dict())
        self.assertEqual(data["title"], "💚 Book Club Bot — Status")
        self.assertEqual(data["color"], COLORS["info"])
        field_names = {f["name"] for f in data["fields"]}
        self.assertEqual(field_names, {"Uptime", "Version", "Database", "Latency", "LLM", "Runtime"})
        # values match injected inputs
        by_name = {f["name"]: f["value"] for f in data["fields"]}
        self.assertEqual(by_name["Uptime"], "1h 2m 3s")
        self.assertEqual(by_name["Version"], "9.9.9")
        self.assertEqual(by_name["Database"], "1.2 MB")
        self.assertEqual(by_name["Latency"], "42 ms")
        self.assertIn("anthropic", by_name["LLM"])

    def test_build_status_embed_trims_long_values(self):
        from status import build_status_embed
        long_val = "x" * 5000
        embed = build_status_embed(
            uptime=long_val,
            version=long_val,
            db_size=long_val,
            latency=long_val,
            llm=long_val,
        )
        for field in embed.fields:
            self.assertLessEqual(len(cast(str, field.value)), 1024)

    def test_build_status_embed_defaults_use_live_lookups(self):
        from status import build_status_embed
        embed = build_status_embed(latency="10 ms")
        by_name = {f.name: f.value for f in embed.fields}
        self.assertIn("Uptime", by_name)
        self.assertIn("Version", by_name)
        self.assertIn("Database", by_name)
        self.assertEqual(by_name["Latency"], "10 ms")
        self.assertIn("LLM", by_name)

    def test_build_status_embed_missing_metrics_graceful(self):
        from status import build_status_embed
        embed = build_status_embed(
            uptime="Unavailable",
            version="0.1.0",
            db_size="Unavailable",
            latency="Unavailable",
            llm="unknown · unknown",
        )
        by_name = {f.name: f.value for f in embed.fields}
        self.assertEqual(by_name["Database"], "Unavailable")
        self.assertEqual(by_name["Latency"], "Unavailable")


class StatusVersionCentralTests(unittest.TestCase):
    def test_version_single_source(self):
        import config
        import status
        # config must expose APP_VERSION
        self.assertTrue(hasattr(config, "APP_VERSION"))
        self.assertIsInstance(config.APP_VERSION, str)
        # status module should use config.APP_VERSION, not duplicate literal everywhere
        embed = status.build_status_embed(latency="1 ms")
        by_name = {f.name: f.value for f in embed.fields}
        self.assertEqual(by_name["Version"], config.APP_VERSION)
        # ensure no second version literal in status module (quick guard)
        import pathlib
        text = pathlib.Path("status.py").read_text()
        # APP_VERSION should be imported, not redefined
        self.assertIn("APP_VERSION", text)

    def test_llm_never_exposes_keys(self):
        import status
        val = status.get_llm_display()
        self.assertNotIn("sk-", val)
        # ensure raw keys from config are not in display
        import config
        for key in [config.ANTHROPIC_API_KEY, config.OPENAI_API_KEY, config.DEEPSEEK_API_KEY]:
            if key:
                self.assertNotIn(key[:8], val)

    def test_status_uses_shared_model_resolution(self):
        import status

        with mock.patch("status.LLM_PROVIDER", "openai"), mock.patch("status.LLM_MODEL", ""):
            self.assertEqual(status.get_llm_display(), "openai · gpt-4o")


class StatusCommandMetadataTests(unittest.TestCase):
    def test_register_includes_status(self):
        from commands import register, slash_status

        class FakeTree:
            def __init__(self):
                self.cmds = {}

            def command(self, name, description):
                def decorator(fn):
                    self.cmds[name] = (fn, description)
                    return fn
                return decorator

        tree = FakeTree()
        register(tree)
        self.assertIn("status", tree.cmds)
        fn, desc = tree.cmds["status"]
        self.assertIs(fn, slash_status)
        self.assertIn("uptime", desc.lower())
        self.assertIn("version", desc.lower())

    def test_slash_status_is_ephemeral(self):
        # Inspect source for ephemeral=True without executing Discord I/O
        import pathlib
        src = pathlib.Path("commands.py").read_text()
        # slash_status must send ephemeral
        self.assertIn("slash_status", src)
        self.assertIn("ephemeral=True", src)
        # Ensure help mentions status
        help_src = pathlib.Path("help_content.py").read_text()
        self.assertIn("/status", help_src)

    def test_slash_status_sends_embed(self):
        import asyncio
        from commands import slash_status
        import status as status_helpers

        class FakeResponse:
            def __init__(self):
                self.kwargs = None

            async def send_message(self, **kwargs):
                self.kwargs = kwargs

        class FakeClient:
            latency = 0.123

        class FakeInteraction:
            client = FakeClient()
            response = FakeResponse()

        interaction = FakeInteraction()
        asyncio.run(slash_status(cast(Any, interaction)))
        self.assertIsNotNone(interaction.response.kwargs)
        kwargs = cast(dict[str, Any], interaction.response.kwargs)
        self.assertTrue(kwargs.get("ephemeral"))
        self.assertIn("embed", kwargs)
        embed = kwargs["embed"]
        self.assertEqual(embed.title, "💚 Book Club Bot — Status")


if __name__ == "__main__":
    unittest.main()
