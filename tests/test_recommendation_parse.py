"""Offline coverage for structured recommendation parsing."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm import format_recommendation_text, parse_recommendation_payload


class RecommendationParseTests(unittest.TestCase):
    def test_parses_structured_json(self):
        raw = """{
          "intro": "Based on your love of Dune:",
          "books": [
            {"title": "Hyperion", "author": "Dan Simmons", "why": "Epic scope and weirdness."},
            {"title": "The Left Hand of Darkness", "author": "Ursula K. Le Guin", "why": "Political intrigue."}
          ]
        }"""
        payload = parse_recommendation_payload(raw)
        self.assertEqual(payload["intro"], "Based on your love of Dune:")
        self.assertEqual(len(payload["books"]), 2)
        self.assertEqual(payload["books"][0]["title"], "Hyperion")

    def test_strips_fence_and_skips_bad_entries(self):
        raw = """```json
        {"intro": "Hi", "books": [{"title": "Good"}, {"title": ""}, "nope", {"author": "Only"}]}
        ```"""
        payload = parse_recommendation_payload(raw)
        self.assertEqual(payload["intro"], "Hi")
        self.assertEqual(payload["books"], [{"title": "Good", "author": None, "why": None}])

    def test_plain_text_fallback(self):
        payload = parse_recommendation_payload("Just try Piranesi sometime.")
        self.assertEqual(payload["intro"], "Just try Piranesi sometime.")
        self.assertEqual(payload["books"], [])
        self.assertEqual(
            format_recommendation_text(payload),
            "Just try Piranesi sometime.",
        )

    def test_format_with_books(self):
        text = format_recommendation_text(
            {
                "intro": "Try these:",
                "books": [
                    {"title": "Piranesi", "author": "Susanna Clarke", "why": "Labyrinth vibes."}
                ],
            }
        )
        self.assertIn("Try these:", text)
        self.assertIn("**Piranesi** by Susanna Clarke", text)
        self.assertIn("Labyrinth vibes.", text)


if __name__ == "__main__":
    unittest.main()
