"""
Make `wiki_interest` (and the top-level `cli` entry-point script) importable
when running pytest from the repo root (`python -m pytest wiki-interest/tests`),
given the src-layout of the wiki-interest package (src/wiki_interest/...)
which isn't pip-installed.
"""

import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # tests/ -> wiki-interest/
_SRC = os.path.join(_ROOT, "src")
for _path in (_SRC, _ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)


@pytest.fixture(autouse=True)
def _default_wikitrend_contact(monkeypatch):
    """
    Give every test an explicit WIKITREND_CONTACT by default -- optional at
    runtime (api_client falls back to DEFAULT_CONTACT, its own repo URL, if
    unset), but tests should assert against a known, explicit value rather
    than depend on that fallback. Tests that specifically exercise the
    unset/custom-contact behavior call monkeypatch.delenv/setenv themselves,
    which overrides this default within that test.
    """
    monkeypatch.setenv("WIKITREND_CONTACT", "https://example.org/wiki-interest; test@example.org")
