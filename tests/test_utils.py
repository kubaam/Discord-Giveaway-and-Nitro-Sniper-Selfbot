import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from main import contains_blacklisted  # noqa: E402


def test_contains_blacklisted_case_insensitive():
    blacklist = ["BadWord"]
    assert contains_blacklisted("this has badword", blacklist)
    assert contains_blacklisted("BADWORD present", blacklist)
    assert not contains_blacklisted("nothing here", blacklist)


def test_contains_blacklisted_empty_string():
    blacklist = ["bad"]
    assert not contains_blacklisted("", blacklist)
    assert not contains_blacklisted(None, blacklist)
