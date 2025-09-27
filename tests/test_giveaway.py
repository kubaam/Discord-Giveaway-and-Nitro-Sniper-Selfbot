import unittest
from types import SimpleNamespace

import main


class FakeReaction:
    def __init__(self, emoji, count, me=False):
        self.emoji = emoji
        self.count = count
        self.me = me


class FakeCustomEmoji:
    def __init__(self, name, emoji_id, animated=False):
        self.name = name
        self.id = emoji_id
        self.animated = animated


class FakeMessage:
    def __init__(self, reactions):
        self.reactions = reactions
        self.components = []
        self.guild = SimpleNamespace(name="Guild", id=1)
        self.channel = SimpleNamespace(name="general", id=2)
        self.author = SimpleNamespace(id=3)
        self.id = 4


class GiveawayHelperTests(unittest.TestCase):
    def test_encode_unicode_emoji(self):
        encoded = main._encode_reaction_emoji_identifier("🎉")
        self.assertEqual(encoded, "%F0%9F%8E%89")

    def test_encode_custom_emoji(self):
        emoji = FakeCustomEmoji("party", 12345, animated=True)
        encoded = main._encode_reaction_emoji_identifier(emoji)
        self.assertEqual(encoded, "a:party:12345")

    def test_collect_existing_reactions_filters_self_and_empty(self):
        reactions = [
            FakeReaction("🎉", 3, me=False),
            FakeReaction("👍", 0, me=False),
            FakeReaction("🔥", 2, me=True),
        ]
        message = FakeMessage(reactions)

        identifiers = main._collect_existing_reaction_identifiers(message)

        self.assertEqual(identifiers, ["%F0%9F%8E%89"])

    def test_create_smart_coro_returns_none_when_no_actions_available(self):
        message = FakeMessage(reactions=[])
        interaction_coro = main.create_smart_giveaway_entry_coro(message)
        self.assertIsNone(interaction_coro)

    def test_create_smart_coro_uses_reactions_when_available(self):
        message = FakeMessage(reactions=[FakeReaction("🎉", 1)])
        interaction_coro = main.create_smart_giveaway_entry_coro(message)
        self.assertTrue(callable(interaction_coro))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
