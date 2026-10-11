"""Offline tests for opt-in member-facing release notes."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from release_notes import (
    OLDER_UPDATES_NOTE,
    format_release_board,
    get_release_announcement,
    latest_changelog_version,
    parse_release_announcements,
    ReleaseAnnouncement,
)


class ReleaseNotesTests(unittest.TestCase):
    def parse(self, text):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as changelog:
            changelog.write(text)
            changelog_path = changelog.name
        try:
            return parse_release_announcements(changelog_path)
        finally:
            os.unlink(changelog_path)

    def test_returns_only_explicit_member_facing_announcements(self):
        announcements = self.parse(
            """## [0.3.1] (Maintenance)

- Updated dependencies.

## [0.3.0] (Easier scheduling)
announce: true

### Announcement
- Vote on the next reading schedule with `/coordination`.
- Browse the improved help hub with `/help`.

### Fixed
- Internal scheduler cleanup.

## [0.2.9] (Incomplete announcement)
announce: true

### Added
- Internal-only implementation detail.
"""
        )

        self.assertEqual(len(announcements), 1)
        self.assertEqual(announcements[0].version, "0.3.0")
        self.assertEqual(announcements[0].title, "Easier scheduling")
        self.assertEqual(
            announcements[0].bullets,
            (
                "Vote on the next reading schedule with `/coordination`.",
                "Browse the improved help hub with `/help`.",
            ),
        )

    def test_app_version_follows_latest_changelog_heading(self):
        self.assertEqual(latest_changelog_version("CHANGELOG.md"), config.APP_VERSION)
        self.assertNotEqual(config.APP_VERSION, "0.1.0")
        current = get_release_announcement(config.APP_VERSION, "CHANGELOG.md")
        if current is not None:
            self.assertEqual(current.version, config.APP_VERSION)
            self.assertTrue(current.bullets)

    def test_latest_changelog_version_reads_first_heading(self):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as changelog:
            changelog.write("## [2026-08-31] (A release)\n\n- note\n\n## [2026-08-30] (older)\n")
            changelog_path = changelog.name
        try:
            self.assertEqual(latest_changelog_version(changelog_path), "2026-08-31")
        finally:
            os.unlink(changelog_path)

    def test_get_release_announcement_selects_one_version(self):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as changelog:
            changelog.write(
                "## [0.3.0] (A release)\nannounce: true\n\n"
                "### Announcement\n- A member-facing improvement.\n"
            )
            changelog_path = changelog.name
        try:
            self.assertIsNotNone(get_release_announcement("0.3.0", changelog_path))
            self.assertIsNone(get_release_announcement("0.3.1", changelog_path))
        finally:
            os.unlink(changelog_path)

    def test_format_release_board_newest_first_and_drops_overflow(self):
        newest = ReleaseAnnouncement("2", "New", ("Short.",))
        older = ReleaseAnnouncement("1", "Old", ("x" * 200,))
        body = format_release_board((newest, older), limit=80)
        self.assertIn("**2 — New**", body)
        self.assertNotIn("**1 — Old**", body)
        self.assertIn(OLDER_UPDATES_NOTE, body)

    def test_format_release_board_trims_a_single_oversized_release(self):
        huge = ReleaseAnnouncement("1", "Huge", ("y" * 50,))
        body = format_release_board((huge,), limit=40)
        self.assertTrue(body.endswith("…"))
        self.assertLessEqual(len(body), 40)
        self.assertNotIn(OLDER_UPDATES_NOTE, body)


if __name__ == "__main__":
    unittest.main()

