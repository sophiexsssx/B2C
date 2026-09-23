"""
Tests for wiki_interest.cache.

No real filesystem writes outside `tmp_path` -- WIKI_INTEREST_CACHE_DIR is
monkeypatched to an isolated tmp dir for every test in this file, so the
real wiki-interest/cache/ is never touched.
"""

import json
import os
import time

import pytest

from wiki_interest import cache


@pytest.fixture(autouse=True)
def _isolated_cache_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_INTEREST_CACHE_DIR", str(tmp_path))
    return tmp_path


def _age_cached_entry(key: str, age_seconds: float) -> None:
    """Rewrite a cached entry's fetched_at to `age_seconds` in the past --
    simulates an old cache entry without sleeping in a test."""
    path = os.path.join(cache.cache_dir(), "api", f"{key}.json")
    with open(path, encoding="utf-8") as f:
        entry = json.load(f)
    entry["fetched_at"] = time.time() - age_seconds
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entry, f)


# ---------------------------------------------------------------------------
# 1-2. get_cached_json / set_cached_json
# ---------------------------------------------------------------------------


def test_get_cached_json_returns_none_for_unknown_key():
    assert cache.get_cached_json("never-cached-key") is None


def test_set_then_get_round_trips_exact_value():
    value = {"month": "2024-01", "views": 42, "nested": [1, 2, 3]}
    cache.set_cached_json("some-key", value)

    assert cache.get_cached_json("some-key") == value


# ---------------------------------------------------------------------------
# 3. cached_call
# ---------------------------------------------------------------------------


def test_cached_call_only_fetches_once_for_the_same_key():
    calls = []

    def fetch():
        calls.append(1)
        return {"result": "fetched"}

    first = cache.cached_call("same-key", fetch)
    second = cache.cached_call("same-key", fetch)

    assert first == {"result": "fetched"}
    assert second == {"result": "fetched"}
    assert len(calls) == 1  # second call served from cache, fetch_fn not called again


def test_cached_call_with_different_key_triggers_a_fresh_fetch():
    calls = []

    def fetch():
        calls.append(1)
        return {"result": f"fetch-{len(calls)}"}

    cache.cached_call("key-a", fetch)
    cache.cached_call("key-b", fetch)

    assert len(calls) == 2  # different key -> not served from key-a's cache entry


# ---------------------------------------------------------------------------
# 4. generate_run_id
# ---------------------------------------------------------------------------


def test_generate_run_id_differs_across_calls():
    first = cache.generate_run_id()
    second = cache.generate_run_id()

    assert first != second


# ---------------------------------------------------------------------------
# 5. save_run_data / load_run_data
# ---------------------------------------------------------------------------


def test_save_and_load_run_data_round_trips_and_creates_run_dir(tmp_path):
    run_id = cache.generate_run_id()
    data = {"topic": "Astronomy", "langs": ["de", "fr"], "results": {"de": {"yoy_growth": 0.5}}}

    cache.save_run_data(run_id, data)

    expected_dir = os.path.join(str(tmp_path), "runs", run_id)
    assert os.path.isdir(expected_dir)
    assert os.path.exists(os.path.join(expected_dir, "run.json"))

    loaded = cache.load_run_data(run_id)
    assert loaded == data


# ---------------------------------------------------------------------------
# 6. max_age_seconds expiry (get_cached_json)
# ---------------------------------------------------------------------------


def test_get_cached_json_treats_an_aged_entry_older_than_max_age_as_a_miss():
    cache.set_cached_json("aging-key", {"v": 1})
    _age_cached_entry("aging-key", age_seconds=999_999)  # far in the past

    assert cache.get_cached_json("aging-key", max_age_seconds=3600) is None


def test_get_cached_json_with_no_max_age_serves_an_aged_entry_forever():
    cache.set_cached_json("aging-key", {"v": 1})
    _age_cached_entry("aging-key", age_seconds=999_999)

    assert cache.get_cached_json("aging-key") == {"v": 1}


def test_get_cached_json_with_max_age_serves_a_fresh_entry():
    cache.set_cached_json("fresh-key", {"v": "not-aged"})

    assert cache.get_cached_json("fresh-key", max_age_seconds=3600) == {"v": "not-aged"}


# ---------------------------------------------------------------------------
# 7. max_age_seconds expiry (cached_call)
# ---------------------------------------------------------------------------


def test_cached_call_refetches_when_the_cached_entry_is_older_than_max_age():
    cache.set_cached_json("aging-key", {"v": "stale"})
    _age_cached_entry("aging-key", age_seconds=999_999)

    calls = []

    def fetch():
        calls.append(1)
        return {"v": "fresh"}

    result = cache.cached_call("aging-key", fetch, max_age_seconds=3600)

    assert len(calls) == 1  # a real refetch happened, not served from stale cache
    assert result == {"v": "fresh"}
    # and the fresh value is what got re-cached
    assert cache.get_cached_json("aging-key") == {"v": "fresh"}


def test_cached_call_serves_a_fresh_entry_without_calling_fetch_fn():
    cache.set_cached_json("fresh-key", {"v": "not-aged"})

    calls = []

    def fetch():
        calls.append(1)
        return {"v": "should-not-be-used"}

    result = cache.cached_call("fresh-key", fetch, max_age_seconds=3600)

    assert len(calls) == 0
    assert result == {"v": "not-aged"}
