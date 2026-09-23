"""
Make `wiki_interest` importable when running pytest from the repo root
(`python -m pytest wiki-interest/tests`), given the src-layout of the
wiki-interest package (src/wiki_interest/...) which isn't pip-installed.
"""

import os
import sys

import pytest

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


@pytest.fixture(autouse=True)
def _default_wikitrend_contact(monkeypatch):
    """
    Give every test a WIKITREND_CONTACT by default, since _get_json() now
    requires one on every request. Tests that specifically exercise the
    missing/custom-contact behavior call monkeypatch.delenv/setenv
    themselves, which overrides this default within that test.
    """
    monkeypatch.setenv("WIKITREND_CONTACT", "https://example.org/wiki-interest; test@example.org")
