"""
Disk cache for API responses, and run-folder management for analyze/report.

notes/requirements.md "My decisions": repeated/related queries should hit a
disk cache instead of refetching, and reports get rebuilt from a saved run
folder. Both live under one root directory (default wiki-interest/cache/,
gitignored) so a single cleanup deletes everything.
"""

import hashlib
import json
import os
import random
import tempfile
import time

_PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # wiki-interest/src -> wiki-interest
DEFAULT_CACHE_DIR = os.path.join(os.path.dirname(_PACKAGE_DIR), "cache")


def cache_dir() -> str:
    """The cache root -- overridable via WIKI_INTEREST_CACHE_DIR (tests use a tmp dir)."""
    return os.environ.get("WIKI_INTEREST_CACHE_DIR", DEFAULT_CACHE_DIR)


def cache_key(**kwargs) -> str:
    """A deterministic key from keyword args (e.g. project/article/agent/start/end)."""
    payload = json.dumps(kwargs, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _atomic_write_json(path: str, payload, indent=None) -> None:
    """
    Write `payload` as JSON to `path` atomically: serialize to a temp file
    in the same directory, then os.replace() it into place. A concurrent
    reader (cached_call is explicitly meant for repeated/parallel fetches;
    `report` reads a run.json that `analyze` wrote) -- or this process
    crashing mid-write -- can then only ever see the old complete file or
    the new complete one, never a truncated or partially-written one, which
    a plain open(path, "w") + json.dump would risk.
    """
    directory = os.path.dirname(path)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def get_cached_json(key: str, max_age_seconds: float = None):
    """
    Return the cached value for `key`, or None if nothing is cached, or if
    it's cached but older than `max_age_seconds` (a cache miss, not an
    error -- the caller re-fetches and overwrites it, same as if nothing
    had been cached at all).

    Entries cache forever by default (max_age_seconds=None): a genuinely
    complete, finalized month of pageviews never changes, so there's no
    reason to ever refetch it. The exception is a range whose `end` is the
    CURRENT in-progress month -- the API returns a real but partial count
    for it (see metrics.is_current_month), and caching that forever would
    silently serve a stale, undercounted figure even after the month is
    finalized weeks later. Callers fetching such a range should pass a
    short max_age_seconds (e.g. 86400) instead of the default.
    """
    path = os.path.join(cache_dir(), "api", f"{key}.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        entry = json.load(f)
    if max_age_seconds is not None and (time.time() - entry["fetched_at"]) > max_age_seconds:
        return None
    return entry["value"]


def set_cached_json(key: str, value) -> None:
    directory = os.path.join(cache_dir(), "api")
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{key}.json")
    _atomic_write_json(path, {"value": value, "fetched_at": time.time()})


def cached_call(key: str, fetch_fn, max_age_seconds: float = None):
    """
    Return the cached value for `key` if present and not older than
    `max_age_seconds`, else call fetch_fn(), cache it, and return it. See
    get_cached_json's docstring for when to pass max_age_seconds.
    """
    cached = get_cached_json(key, max_age_seconds=max_age_seconds)
    if cached is not None:
        return cached
    value = fetch_fn()
    set_cached_json(key, value)
    return value


def generate_run_id() -> str:
    """A server-generated run id -- callers (the agent) never supply their own (notes/plan.md)."""
    return f"r_{time.strftime('%Y%m%d')}_{random.randint(0, 0xFFFFFF):06x}"


def run_dir(run_id: str) -> str:
    path = os.path.join(cache_dir(), "runs", run_id)
    os.makedirs(path, exist_ok=True)
    return path


def save_run_data(run_id: str, data: dict) -> str:
    """
    Reserve `run_id`'s directory exclusively and write `data` as its
    run.json, retrying with a freshly generated run_id if the one given is
    already taken.

    generate_run_id()'s random suffix (24 bits, refreshed daily) can
    theoretically collide -- a real risk across many runs generated in one
    day, e.g. an eval batch. run_dir() uses exist_ok=True so load_run_data
    can open an existing run's directory, but that same leniency would let
    a brand-new run silently overwrite an unrelated old run's saved data on
    collision, with no error. This reserves the directory with exclusive
    creation first and retries on FileExistsError instead.

    Returns the run_id actually used -- equal to the one passed in unless a
    collision forced a retry; callers must use this return value, not just
    the run_id they originally passed in.
    """
    while True:
        path = os.path.join(cache_dir(), "runs", run_id)
        try:
            os.makedirs(path, exist_ok=False)
            break
        except FileExistsError:
            run_id = generate_run_id()
    _atomic_write_json(os.path.join(path, "run.json"), data, indent=2)
    return run_id


def load_run_data(run_id: str) -> dict:
    path = os.path.join(run_dir(run_id), "run.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)
