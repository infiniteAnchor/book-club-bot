import unittest
from typing import cast

from help_content import build_help_embed, build_unknown_help_embed


class HelpContentTests(unittest.TestCase):
    def test_member_hub_excludes_admin_section(self):
        embed = build_help_embed(is_admin=False)
        fields = {field.name for field in embed.fields}
        self.assertIn("📚 Books", fields)
        self.assertNotIn("🛠️ Admin", fields)

    def test_admin_hub_includes_admin_section(self):
        embed = build_help_embed(is_admin=True)
        self.assertIn("🛠️ Admin", {field.name for field in embed.fields})

    def test_admin_topic_falls_back_for_non_admin(self):
        embed = build_help_embed("admin", is_admin=False)
        self.assertEqual(embed.title, "📚 Book Club Bot help")

    def test_member_topic_has_examples(self):
        embed = build_help_embed("polls")
        self.assertEqual(embed.title, "🗳️ Help: nominations and polls")
        self.assertIn("`Nominate Piranesi", cast(str, embed.fields[0].value))
        self.assertIn("/nominate", cast(str, embed.fields[0].value))
        self.assertIn("Club tier", cast(str, embed.fields[0].value))

    def test_membership_topic_names_four_tiers_and_separates_nudges(self):
        embed = build_help_embed("membership")
        self.assertEqual(embed.title, "⚙️ Help: membership and nudges")
        text = "\n".join(
            [embed.description or "", *(cast(str, field.value) for field in embed.fields)]
        )
        for name in ("Tracking", "Updates", "Club", "Chat only"):
            self.assertIn(name, text)
        self.assertIn("not DM nudges", text)
        self.assertIn("independent of Updates", text)

    def test_hub_membership_names_tiers(self):
        embed = build_help_embed()
        membership = next(
            cast(str, field.value) for field in embed.fields if "Membership" in (field.name or "")
        )
        self.assertIn("Tracking", membership)
        self.assertIn("Chat only", membership)

    def test_club_topic_covers_current_read(self):
        embed = build_help_embed("club")
        value = "\n".join(field.value for field in embed.fields)
        self.assertIn("What are we reading?", value)
        self.assertIn("/current", value)

    def test_books_topic_covers_queue_command_and_remove(self):
        embed = build_help_embed("books")
        value = "\n".join(field.value for field in embed.fields)
        self.assertIn("/queue", value)
        self.assertIn("Remove Piranesi from my queue", value)
        self.assertIn("/import", value)
        self.assertIn("/export", value)
        self.assertIn("Export Library", value)

    def test_unenrolled_and_dm_guidance(self):
        unenrolled = build_help_embed(is_enrolled=False, welcome_channel="#welcome")
        dm = build_help_embed(is_dm=True, welcome_channel="#welcome")
        self.assertIn("Join the book club", {field.name for field in unenrolled.fields})
        self.assertIn("Join from your server", {field.name for field in dm.fields})

    def test_unknown_fallback_points_to_both_help_entrypoints(self):
        embed = build_unknown_help_embed("Could you rephrase?")
        self.assertIn("/help", cast(str, embed.fields[1].value))


if __name__ == "__main__":
    unittest.main()
