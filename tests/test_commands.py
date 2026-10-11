import os
import sys
import unittest

import discord

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from commands import (
    COMMAND_DESCRIPTIONS,
    DISCORD_DESCRIPTION_MAX,
    OPTION_DESCRIPTIONS,
    register,
    slash_admin_dashboard,
    slash_coordination,
    slash_help,
    slash_nominate,
    slash_nominations,
    slash_poll,
    slash_current,
    slash_queue,
    slash_status,
    slash_history,
    slash_scrapbook,
    slash_import,
    slash_export,
)
from views_polls import (
    NOMINATION_AUTHOR_MAX_LENGTH,
    NOMINATION_REASON_MAX_LENGTH,
    NOMINATION_TITLE_MAX_LENGTH,
)


COMMAND_HANDLERS = {
    "help": slash_help,
    "status": slash_status,
    "admin-dashboard": slash_admin_dashboard,
    "nominations": slash_nominations,
    "nominate": slash_nominate,
    "poll": slash_poll,
    "coordination": slash_coordination,
    "queue": slash_queue,
    "current": slash_current,
    "history": slash_history,
    "scrapbook": slash_scrapbook,
    "import": slash_import,
    "export": slash_export,
}


class FakeTree:
    def __init__(self):
        self.cmds = {}

    def command(self, name, description):
        def decorator(fn):
            self.cmds[name] = (fn, description)
            return fn
        return decorator


class CommandDescriptionTests(unittest.TestCase):
    def test_command_descriptions_fit_discord_limit(self):
        self.assertEqual(set(COMMAND_DESCRIPTIONS), set(COMMAND_HANDLERS))
        for name, description in COMMAND_DESCRIPTIONS.items():
            self.assertIsInstance(description, str, name)
            self.assertGreaterEqual(len(description), 1, name)
            self.assertLessEqual(
                len(description),
                DISCORD_DESCRIPTION_MAX,
                f"{name} is {len(description)} chars: {description!r}",
            )

    def test_option_descriptions_fit_discord_limit(self):
        for command, options in OPTION_DESCRIPTIONS.items():
            self.assertIn(command, COMMAND_DESCRIPTIONS)
            for option, description in options.items():
                label = f"{command}.{option}"
                self.assertIsInstance(description, str, label)
                self.assertGreaterEqual(len(description), 1, label)
                self.assertLessEqual(
                    len(description),
                    DISCORD_DESCRIPTION_MAX,
                    f"{label} is {len(description)} chars: {description!r}",
                )

    def test_register_uses_command_descriptions(self):
        tree = FakeTree()
        register(tree)
        self.assertEqual(set(tree.cmds), set(COMMAND_DESCRIPTIONS))
        for name, (fn, description) in tree.cmds.items():
            self.assertIs(fn, COMMAND_HANDLERS[name])
            self.assertEqual(description, COMMAND_DESCRIPTIONS[name])

    def test_option_descriptions_are_wired(self):
        self.assertEqual(
            slash_help.__discord_app_commands_param_description__,
            OPTION_DESCRIPTIONS["help"],
        )
        self.assertEqual(
            slash_nominations.__discord_app_commands_param_description__,
            OPTION_DESCRIPTIONS["nominations"],
        )
        self.assertEqual(
            slash_nominate.__discord_app_commands_param_description__,
            OPTION_DESCRIPTIONS["nominate"],
        )
        self.assertEqual(
            slash_poll.__discord_app_commands_param_description__,
            OPTION_DESCRIPTIONS["poll"],
        )
        self.assertEqual(
            slash_coordination.__discord_app_commands_param_description__,
            OPTION_DESCRIPTIONS["coordination"],
        )
        self.assertEqual(
            slash_queue.__discord_app_commands_param_description__,
            OPTION_DESCRIPTIONS["queue"],
        )
        self.assertEqual(
            slash_current.__discord_app_commands_param_description__,
            OPTION_DESCRIPTIONS["current"],
        )
        self.assertEqual(
            slash_import.__discord_app_commands_param_description__,
            OPTION_DESCRIPTIONS["import"],
        )

    def test_nominate_option_lengths_match_modal_limits(self):
        command = discord.app_commands.Command(
            name="nominate",
            description=COMMAND_DESCRIPTIONS["nominate"],
            callback=slash_nominate,
        )
        options = {
            parameter.name: parameter._Parameter__parent.to_dict()
            for parameter in command.parameters
        }
        self.assertEqual(options["title"]["max_length"], NOMINATION_TITLE_MAX_LENGTH)
        self.assertEqual(options["author"]["max_length"], NOMINATION_AUTHOR_MAX_LENGTH)
        self.assertEqual(options["reason"]["max_length"], NOMINATION_REASON_MAX_LENGTH)

    def test_action_choice_labels_are_specific(self):
        nominations = slash_nominations.__discord_app_commands_param_choices__["action"]
        self.assertEqual(
            [(choice.name, choice.value) for choice in nominations],
            [
                ("Open nominations", "open"),
                ("Close nominations", "close"),
                ("List nominations", "list"),
            ],
        )
        poll = slash_poll.__discord_app_commands_param_choices__["action"]
        self.assertEqual(
            [(choice.name, choice.value) for choice in poll],
            [
                ("Open book poll", "open"),
                ("Close book poll", "close"),
                ("View results", "results"),
            ],
        )
        coordination = slash_coordination.__discord_app_commands_param_choices__["action"]
        self.assertEqual(
            [(choice.name, choice.value) for choice in coordination],
            [
                ("Open scheduling poll", "open"),
                ("Close scheduling poll", "close"),
                ("View results", "results"),
            ],
        )
        queue = slash_queue.__discord_app_commands_param_choices__["action"]
        self.assertEqual(
            [(choice.name, choice.value) for choice in queue],
            [
                ("List privately", "list"),
                ("Add a book", "add"),
                ("Remove a book", "remove"),
            ],
        )
        current = slash_current.__discord_app_commands_param_choices__["action"]
        self.assertEqual(
            [(choice.name, choice.value) for choice in current],
            [
                ("Show current book", "show"),
                ("Start club book", "start"),
                ("Set target date", "target"),
                ("Complete club book", "complete"),
            ],
        )

    def test_command_copy_distinguishes_the_three_poll_workflows(self):
        nominations = COMMAND_DESCRIPTIONS["nominations"].lower()
        poll = COMMAND_DESCRIPTIONS["poll"].lower()
        coordination = COMMAND_DESCRIPTIONS["coordination"].lower()
        self.assertIn("nominat", nominations)
        self.assertNotIn("schedule", nominations)
        self.assertIn("vote", poll)
        self.assertIn("nominat", poll)
        self.assertIn("schedule", coordination)
        self.assertNotEqual(nominations, poll)
        self.assertNotEqual(poll, coordination)
        self.assertNotEqual(nominations, coordination)


if __name__ == "__main__":
    unittest.main()
