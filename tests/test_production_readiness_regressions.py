import builtins
import sqlite3

import pytest

import main
from ivy_core.pipeline_status import ProviderUnavailableError, RetryableProviderError
from proactive_agents import sports_bettor


def _reset_favorites_cache() -> None:
    main._FAVORITES_CACHE_STATE = None
    main._FAVORITES_CACHE_CONTACTS = None
    main._FAVORITES_CACHE_MTIME_NS = None
    main._FAVORITES_CACHE_SIZE = None
    main._FAVORITES_WARNED_INVALID_PATHS.clear()


def test_load_favorites_cached_uses_project_root_and_caches(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    favorites_path = repo_root / "favorites.json"
    favorites_path.write_text('["+15555550123"]', encoding="utf-8")

    monkeypatch.setattr(main, "PROJECT_ROOT_DIR", str(repo_root))
    monkeypatch.setattr(main, "_get_project_root", lambda: repo_root)
    monkeypatch.chdir(tmp_path)
    _reset_favorites_cache()

    open_calls = 0
    original_open = builtins.open

    def track_favorites_open(path, *args, **kwargs):
        nonlocal open_calls
        if str(path) == str(favorites_path):
            open_calls += 1
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", track_favorites_open)

    assert main.load_favorites_cached() == frozenset(["+15555550123"])
    assert main.load_favorites_cached() == frozenset(["+15555550123"])
    assert open_calls == 1


def test_safe_fetch_last_message_retries_and_recovers(monkeypatch):
    expected_row = (101, "hello", "+15555550123")

    class FailingCursor:
        def execute(self, *_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

    class FailingConnection:
        def cursor(self):
            return FailingCursor()

    class GoodCursor:
        def execute(self, *_args, **_kwargs):
            return None

        def fetchone(self):
            return expected_row

    class GoodConnection:
        def cursor(self):
            return GoodCursor()

    connections = iter([FailingConnection(), GoodConnection()])
    reset_calls = []

    monkeypatch.setattr(main, "init_chat_db", lambda: next(connections))
    monkeypatch.setattr(main, "_close_chat_db_locked", lambda: reset_calls.append("reset"))
    monkeypatch.setattr(main.time, "sleep", lambda *_args, **_kwargs: None)

    assert main.safe_fetch_last_message(100) == expected_row
    assert reset_calls == ["reset"]


def test_close_chat_db_connection_closes_and_clears_cached_connection(monkeypatch):
    class TrackingLock:
        def __init__(self):
            self.enter_count = 0

        def __enter__(self):
            self.enter_count += 1
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

    class Connection:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    lock = TrackingLock()
    conn = Connection()
    monkeypatch.setattr(main, "_CHAT_DB_LOCK", lock)
    monkeypatch.setattr(main, "_CHAT_DB_CONN", conn)

    main.close_chat_db()

    assert lock.enter_count == 1
    assert conn.closed is True
    assert main._CHAT_DB_CONN is None


def test_fetch_live_odds_raises_retryable_error_when_mixed_failures(monkeypatch):
    class FakeFuture:
        def __init__(self, *, exc=None, value=None):
            self.exc = exc
            self.value = value

        def result(self):
            if self.exc is not None:
                raise self.exc
            return self.value

    queued_futures = [
        FakeFuture(exc=ProviderUnavailableError(provider="odds_api", message="timeout")),
        FakeFuture(
            exc=RetryableProviderError(
                provider="odds_api",
                status_code=429,
                message="rate limited",
                retry_after=60,
            )
        ),
    ]

    class FakeExecutor:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def submit(self, _fn, *_args, **_kwargs):
            return queued_futures.pop(0)

    monkeypatch.setattr(sports_bettor, "ODDS_API_KEY", "test-key")
    monkeypatch.setattr(sports_bettor, "ODDS_SPORT_KEYS", {"NFL": "nfl", "MLB": "mlb"})
    monkeypatch.setattr(sports_bettor, "ThreadPoolExecutor", FakeExecutor)
    monkeypatch.setattr(sports_bettor, "as_completed", lambda futures: list(futures))

    with pytest.raises(RetryableProviderError):
        sports_bettor.fetch_live_odds()
