"""Offline tests for global and per-guild release-announcement controls."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import actions_social
import database


class ReleaseAnnouncementSettingsTests(unittest.TestCase):
    def test_global_switch_and_guild_preference_are_both_required(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "bookclub.db")
            with mock.patch.object(database, "DB_PATH", db_path):
                database.init_db()
                with mock.patch.object(config := actions_social.config, "RELEASE_ANNOUNCEMENTS_ENABLED", True):
                    self.assertTrue(actions_social.release_announcements_enabled("guild-1"))
                    self.assertIsNone(actions_social.get_guild_release_announcement_preference("guild-1"))

                    actions_social.set_guild_release_announcements_enabled("guild-1", False)
                    self.assertFalse(actions_social.release_announcements_enabled("guild-1"))

                    actions_social.set_guild_release_announcements_enabled("guild-1", True)
                    self.assertTrue(actions_social.release_announcements_enabled("guild-1"))

                with mock.patch.object(config, "RELEASE_ANNOUNCEMENTS_ENABLED", False):
                    self.assertFalse(actions_social.release_announcements_enabled("guild-1"))


if __name__ == "__main__":
    unittest.main()
