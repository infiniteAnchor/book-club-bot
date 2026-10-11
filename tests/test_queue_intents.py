"""Offline parser plumbing checks for the queue and wishlist NL examples.

These use synthetic model payloads and do not assert how a live provider will
classify the natural-language messages. Real provider behavior belongs in the
opt-in live suite and captured fixtures.
"""
import json
import os
import sys
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _path in (_ROOT, _HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import llm
from intent_cases import QUEUE_REGRESSION_CASES


class QueueIntentParserTests(unittest.TestCase):

    def test_synthetic_queue_cases_parse_to_expected_intents(self):
        for message, expected_action, expected_args in QUEUE_REGRESSION_CASES:
            with self.subTest(message=message):
                payload = {
                    "action": expected_action,
                    "args": expected_args,
                    "response": "Understood.",
                    "flag_injection": False,
                }
                with mock.patch("llm._call_llm", return_value=json.dumps(payload)) as call:
                    result = llm.get_intent(message, {})

                self.assertEqual(result, payload)
                self.assertEqual(call.call_count, 1)


if __name__ == "__main__":
    unittest.main()
