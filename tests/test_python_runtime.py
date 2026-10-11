import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class PythonRuntimeTests(unittest.TestCase):
    def test_get_python_runtime_reports_version_and_impl(self):
        from status import get_python_runtime
        value = get_python_runtime()
        self.assertIn("Python", value)
        # version is dotted numbers
        parts = value.split()
        self.assertTrue(parts[-1][0].isdigit())

    def test_format_python_runtime_graceful(self):
        from status import format_python_runtime
        self.assertEqual(format_python_runtime(None), "Unavailable")
        self.assertEqual(format_python_runtime(""), "Unavailable")
        self.assertEqual(format_python_runtime("CPython 3.12.13"), "CPython 3.12.13")
        self.assertEqual(format_python_runtime("   "), "Unavailable")

    def test_status_embed_includes_runtime_field(self):
        from status import build_status_embed
        embed = build_status_embed(
            uptime="1d 0h 0m",
            db_size="1.0 KB",
            latency="42 ms",
            llm="test · model",
            python_runtime="CPython 3.12.13",
        )
        names = [f.name for f in embed.fields]
        self.assertIn("Runtime", names)


if __name__ == "__main__":
    unittest.main()
