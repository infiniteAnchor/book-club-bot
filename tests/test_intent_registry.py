"""Offline tests for the natural-language intent registry.

Locks handler dispatch, admin/guest flags, and llm.VALID_ACTIONS together so a
new action cannot ship without explicit authorization flags. Admin denial still
lives in the handlers — this file only guards that those checks stay present.
"""
import ast
import os
import pathlib
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from handlers import (
    ADMIN_ONLY_ACTIONS,
    GUEST_BLOCKED_ACTIONS,
    INTENT_ACTIONS,
    OPT_IN_EXEMPT_ACTIONS,
    _ACTION_HANDLERS,
)
from llm import VALID_ACTIONS


EXPECTED_ADMIN_ONLY = frozenset({
    "open_nominations",
    "close_nominations",
    "open_poll",
    "close_poll",
    "open_coordination_poll",
    "close_coordination_poll",
    "admin_dashboard",
    "set_tone",
    "clear_tone",
    "list_tones",
    "manage_club_read",
})

EXPECTED_GUEST_BLOCKED = frozenset({
    "scrapbook",
    "scrapbook_add",
    "scrapbook_remove",
    "add_book",
    "start_book",
    "add_to_wishlist",
    "remove_from_wishlist",
    "queue_add",
    "queue_remove",
    "export_shelf",
    "update_progress",
    "finish_book",
    "dnf_book",
    "rate_book",
    "set_goal",
    "nominate",
    "open_thread",
})


class IntentRegistryTests(unittest.TestCase):
    def test_registry_matches_llm_actions_except_silent(self):
        self.assertEqual(VALID_ACTIONS - {"silent"}, set(INTENT_ACTIONS))

    def test_dispatch_table_is_derived_from_registry(self):
        self.assertEqual(set(_ACTION_HANDLERS), set(INTENT_ACTIONS))
        for name, spec in INTENT_ACTIONS.items():
            self.assertIs(_ACTION_HANDLERS[name], spec.handler)

    def test_admin_and_guest_sets_match_expected(self):
        self.assertEqual(ADMIN_ONLY_ACTIONS, EXPECTED_ADMIN_ONLY)
        self.assertEqual(GUEST_BLOCKED_ACTIONS, EXPECTED_GUEST_BLOCKED)
        self.assertEqual(
            OPT_IN_EXEMPT_ACTIONS,
            ADMIN_ONLY_ACTIONS | {"manage_membership", "help", "current_read"},
        )
        self.assertFalse(ADMIN_ONLY_ACTIONS & GUEST_BLOCKED_ACTIONS)

    def test_admin_only_handlers_still_check_the_role(self):
        src = pathlib.Path("handlers.py").read_text()
        tree = ast.parse(src)
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef)
        }
        for name, spec in INTENT_ACTIONS.items():
            if not spec.admin_only:
                continue
            self.assertIsNotNone(spec.handler, name)
            func_name = spec.handler.__name__
            node = functions[func_name]
            body = ast.get_source_segment(src, node) or ""
            self.assertIn(
                "has_admin_role",
                body,
                f"{name} ({func_name}) is admin_only but does not call has_admin_role",
            )

    def test_nominate_handler_checks_club_tier(self):
        src = pathlib.Path("handlers.py").read_text()
        tree = ast.parse(src)
        nominate = next(
            node for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_handle_nominate"
        )
        body = ast.get_source_segment(src, nominate) or ""
        self.assertIn("has_club", body)

    def test_bot_uses_shared_authorization_sets(self):
        src = pathlib.Path("bot.py").read_text()
        self.assertIn("from handlers import", src)
        self.assertIn("GUEST_BLOCKED_ACTIONS", src)
        self.assertIn("OPT_IN_EXEMPT_ACTIONS", src)
        self.assertNotIn("ADMIN_ONLY_ACTIONS", src)
        self.assertNotRegex(src, r"GUEST_BLOCKED_ACTIONS\s*=")


if __name__ == "__main__":
    unittest.main()
