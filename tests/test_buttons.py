import os
import sys

import discord

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from main import extract_first_button  # noqa: E402


class DummyComponent:
    def __init__(self, comp_type):
        self.type = comp_type


class DummyRow:
    def __init__(self, children):
        self.children = children


class DummyMessage:
    def __init__(self, components):
        self.components = components


def test_extract_first_button_found():
    button = DummyComponent(discord.ComponentType.button)
    msg = DummyMessage([DummyRow([button])])
    assert extract_first_button(msg) is button


def test_extract_first_button_none():
    msg = DummyMessage([DummyRow([DummyComponent(discord.ComponentType.text_input)])])
    assert extract_first_button(msg) is None
