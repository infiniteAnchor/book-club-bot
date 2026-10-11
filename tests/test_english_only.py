import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm import should_enforce_english_only, english_only_intent


class EnglishOnlyGuardrailTests(unittest.TestCase):
    def test_allows_normal_english_message(self):
        self.assertFalse(should_enforce_english_only("I finished Dune last night"))

    def test_blocks_non_latin_script(self):
        self.assertTrue(should_enforce_english_only("Hola, quiero recomendar un libro"))
        self.assertTrue(should_enforce_english_only("こんにちは、おすすめの本ある？"))

    def test_blocks_obvious_non_english_latin_text(self):
        self.assertTrue(should_enforce_english_only("bonjour merci je voudrais un livre"))

    def test_english_only_intent_shape(self):
        intent = english_only_intent()
        self.assertEqual(intent["action"], "unknown")
        self.assertEqual(intent["args"], {})
        self.assertFalse(intent["flag_injection"])
        self.assertIn("English", intent["response"])


if __name__ == "__main__":
    unittest.main()
