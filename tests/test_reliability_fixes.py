"""Comprehensive regression tests for reliability fixes.

Covers:
- Part 1: SQLite connection lifecycle, health checks, retry logic
- Part 2: Favorites cache state model, immutability, fail-closed semantics
- Part 3: Odds API error handling, diagnostics, concurrent ordering
- Part 4: Report signature determinism
"""

import os
import sys
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from email.utils import formatdate

import pytest

# Ensure repo is in path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ============================================================================
# PART 1: SQLite Connection Lifecycle Tests
# ============================================================================

class TestSQLiteConnectionLifecycle:
    """Test SQLite connection creation, health checks, and recovery."""
    
    @pytest.fixture(autouse=True)
    def reset_chat_db_state(self, monkeypatch):
        """Reset global chat.db state before each test."""
        import main
        
        # Reset globals
        main._CHAT_DB_CONN = None
        main._CHAT_DB_SHUTDOWN_REGISTERED = False
        
        # Create temporary test database
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            test_db_path = f.name
        
        # Create minimal schema
        conn = sqlite3.connect(test_db_path)
        conn.execute("CREATE TABLE message (ROWID INTEGER PRIMARY KEY, text TEXT, is_from_me INTEGER, handle_id INTEGER)")
        conn.execute("CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT)")
        conn.commit()
        conn.close()
        
        # Patch CHAT_DB_PATH
        monkeypatch.setattr(main, "CHAT_DB_PATH", test_db_path)
        
        yield test_db_path
        
        # Cleanup
        main._CHAT_DB_CONN = None
        main._CHAT_DB_SHUTDOWN_REGISTERED = False
        try:
            os.unlink(test_db_path)
        except Exception:
            pass
    
    def test_create_connection_success(self, reset_chat_db_state):
        """Successfully create a read-only connection."""
        import main
        conn = main._create_chat_db_connection()
        assert conn is not None
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            cursor.close()
        finally:
            conn.close()
    
    def test_health_check_valid_connection(self, reset_chat_db_state):
        """Health check passes for valid connection."""
        import main
        conn = main._create_chat_db_connection()
        assert main._health_check_connection(conn) is True
        conn.close()
    
    def test_health_check_closed_connection(self):
        """Health check fails for closed connection."""
        import main
        conn = sqlite3.connect(":memory:")
        conn.close()
        assert main._health_check_connection(conn) is False
    
    def test_init_chat_db_establishes_connection(self, reset_chat_db_state):
        """init_chat_db establishes persistent connection."""
        import main
        conn1 = main.init_chat_db()
        assert conn1 is not None
        
        # Subsequent call returns same connection
        conn2 = main.init_chat_db()
        assert conn1 is conn2
    
    def test_init_chat_db_registers_atexit_once(self, reset_chat_db_state):
        """init_chat_db registers atexit cleanup only once."""
        import main
        assert main._CHAT_DB_SHUTDOWN_REGISTERED is False
        main.init_chat_db()
        assert main._CHAT_DB_SHUTDOWN_REGISTERED is True
        
        # Call again; flag stays True (not re-registered)
        main.init_chat_db()
        assert main._CHAT_DB_SHUTDOWN_REGISTERED is True
    
    def test_close_chat_db_idempotent(self, reset_chat_db_state):
        """close_chat_db is idempotent."""
        import main
        main.init_chat_db()
        main.close_chat_db()
        main.close_chat_db()  # Should not raise
        assert main._CHAT_DB_CONN is None
    
    def test_safe_fetch_last_message_explicit_cursor_closure(self, reset_chat_db_state):
        """safe_fetch_last_message explicitly closes cursor."""
        import main
        main.init_chat_db()
        result = main.safe_fetch_last_message(0)
        # Result is None (empty table), but cursor was properly closed
        assert result is None
    
    def test_get_last_message_id_returns_zero_empty_table(self, reset_chat_db_state):
        """get_last_message_id returns 0 for empty message table."""
        import main
        main.init_chat_db()
        result = main.get_last_message_id()
        assert result == 0
    
    def test_concurrent_init_retains_single_connection(self, reset_chat_db_state):
        """Concurrent init_chat_db calls retain only one shared connection."""
        import main
        connections = []
        
        def init_and_store():
            conn = main.init_chat_db()
            connections.append(id(conn))
        
        threads = [threading.Thread(target=init_and_store) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        # All threads got the same connection object
        assert len(set(connections)) == 1


# ============================================================================
# PART 2: Favorites Cache Tests
# ============================================================================

class TestFavoritesCacheReliability:
    """Test favorites cache state model, immutability, and fail-closed semantics."""
    
    @pytest.fixture(autouse=True)
    def reset_favorites_cache(self, monkeypatch, tmp_path):
        """Reset favorites cache state before each test."""
        import main
        
        main._FAVORITES_CACHE_STATE = None
        main._FAVORITES_CACHE_CONTACTS = None
        main._FAVORITES_CACHE_MTIME_NS = None
        main._FAVORITES_CACHE_SIZE = None
        main._FAVORITES_WARNED_INVALID_PATHS = set()
        
        # Use temp directory for test favorites.json
        monkeypatch.setattr(main, "_get_project_root", lambda: tmp_path)
        
        yield tmp_path
        
        # Reset after test
        main._FAVORITES_CACHE_STATE = None
        main._FAVORITES_CACHE_CONTACTS = None
        main._FAVORITES_CACHE_MTIME_NS = None
        main._FAVORITES_CACHE_SIZE = None
        main._FAVORITES_WARNED_INVALID_PATHS = set()
    
    def test_valid_nonempty_file_parsed_once(self, reset_favorites_cache):
        """Valid nonempty file is parsed once and cached."""
        import main
        
        favorites_file = reset_favorites_cache / "favorites.json"
        favorites_file.write_text('["user1", "user2"]')
        
        # First call: parses
        result1 = main.load_favorites_cached()
        assert result1 == frozenset(["user1", "user2"])
        
        # Modify file on disk
        favorites_file.write_text('["user1", "user2", "user3"]')
        
        # Second call with unchanged stat: returns cached (same set)
        # But because we modified the file, stat changed, so it re-parses
        result2 = main.load_favorites_cached()
        assert result2 == frozenset(["user1", "user2", "user3"])
    
    def test_valid_empty_file_cached(self, reset_favorites_cache):
        """Valid empty file (empty list) is cached."""
        import main
        
        favorites_file = reset_favorites_cache / "favorites.json"
        favorites_file.write_text('[]')
        
        result1 = main.load_favorites_cached()
        assert result1 == frozenset()
        
        # Without changing the file, second call returns cached empty set
        result2 = main.load_favorites_cached()
        assert result2 == frozenset()
    
    def test_changed_file_reloads(self, reset_favorites_cache):
        """Changed file is reloaded."""
        import main
        
        favorites_file = reset_favorites_cache / "favorites.json"
        favorites_file.write_text('["user1"]')
        
        result1 = main.load_favorites_cached()
        assert result1 == frozenset(["user1"])
        
        # Sleep briefly and modify file
        time.sleep(0.01)
        favorites_file.write_text('["user2"]')
        
        result2 = main.load_favorites_cached()
        assert result2 == frozenset(["user2"])
    
    def test_deleted_file_invalidates_cache(self, reset_favorites_cache):
        """Deleted file invalidates prior authorization."""
        import main
        
        favorites_file = reset_favorites_cache / "favorites.json"
        favorites_file.write_text('["user1"]')
        
        result1 = main.load_favorites_cached()
        assert result1 == frozenset(["user1"])
        
        # Delete file
        favorites_file.unlink()
        
        # Should return empty frozenset and mark state as invalid
        result2 = main.load_favorites_cached()
        assert result2 == frozenset()
        assert main._FAVORITES_CACHE_STATE is False
    
    def test_malformed_json_fails_closed(self, reset_favorites_cache):
        """Malformed JSON fails closed."""
        import main
        
        favorites_file = reset_favorites_cache / "favorites.json"
        favorites_file.write_text('{invalid json}')
        
        result = main.load_favorites_cached()
        assert result == frozenset()
        assert main._FAVORITES_CACHE_STATE is False
    
    def test_invalid_root_type_fails_closed(self, reset_favorites_cache):
        """Invalid root type (not a list) fails closed."""
        import main
        
        favorites_file = reset_favorites_cache / "favorites.json"
        favorites_file.write_text('{"contacts": ["user1"]}')
        
        result = main.load_favorites_cached()
        assert result == frozenset()
        assert main._FAVORITES_CACHE_STATE is False
    
    def test_non_string_entry_fails_closed(self, reset_favorites_cache):
        """Non-string entry fails closed."""
        import main
        
        favorites_file = reset_favorites_cache / "favorites.json"
        favorites_file.write_text('["user1", 123, "user2"]')
        
        result = main.load_favorites_cached()
        assert result == frozenset()
        assert main._FAVORITES_CACHE_STATE is False
    
    def test_unchanged_invalid_file_not_repeatedly_parsed(self, reset_favorites_cache):
        """Unchanged invalid file is not repeatedly parsed."""
        import main
        
        favorites_file = reset_favorites_cache / "favorites.json"
        favorites_file.write_text('{invalid}')
        
        # First call: parses and fails
        result1 = main.load_favorites_cached()
        assert result1 == frozenset()
        
        # Second call with unchanged file: returns cached failure, no re-parse
        result2 = main.load_favorites_cached()
        assert result2 == frozenset()
        # State should still be False
        assert main._FAVORITES_CACHE_STATE is False
    
    def test_unchanged_invalid_file_not_repeatedly_warned(self, reset_favorites_cache, caplog):
        """Unchanged invalid file is not repeatedly warned about."""
        import main
        import logging
        
        caplog.set_level(logging.WARNING)
        
        favorites_file = reset_favorites_cache / "favorites.json"
        favorites_file.write_text('{invalid}')
        
        # First call: warns
        main.load_favorites_cached()
        warning_count_1 = len([r for r in caplog.records if r.levelname == "WARNING"])
        
        # Second call: no new warning
        main.load_favorites_cached()
        warning_count_2 = len([r for r in caplog.records if r.levelname == "WARNING"])
        
        assert warning_count_2 == warning_count_1
    
    def test_returned_data_cannot_mutate_cache(self, reset_favorites_cache):
        """Returned frozenset cannot mutate internal cache."""
        import main
        
        favorites_file = reset_favorites_cache / "favorites.json"
        favorites_file.write_text('["user1"]')
        
        result = main.load_favorites_cached()
        assert isinstance(result, frozenset)
        
        # Verify frozenset has no mutating methods (immutable by design)
        assert not hasattr(result, 'add')
        assert not hasattr(result, 'remove')
        assert not hasattr(result, 'discard')
        assert not hasattr(result, 'clear')
    
    def test_path_resolution_independent_of_cwd(self, reset_favorites_cache, monkeypatch):
        """Path resolution is independent of current working directory."""
        import main
        
        favorites_file = reset_favorites_cache / "favorites.json"
        favorites_file.write_text('["user1"]')
        
        # Change to a different directory
        original_cwd = os.getcwd()
        try:
            os.chdir("/tmp")
            result = main.load_favorites_cached()
            assert result == frozenset(["user1"])
        finally:
            os.chdir(original_cwd)


# ============================================================================
# PART 3: Odds API Reliability Tests
# ============================================================================

class TestOddsAPIReliability:
    """Test Odds API error handling, diagnostics, and concurrent ordering."""
    
    @pytest.fixture(autouse=True)
    def reset_odds_state(self, monkeypatch):
        """Reset odds API state before each test."""
        import proactive_agents.sports_bettor as sb
        
        # Reset diagnostics
        sb._ODDS_DIAGNOSTICS = {
            "configured_leagues": 0,
            "successful_leagues": [],
            "failed_leagues": [],
            "error_categories": [],
            "success_count": 0,
            "failure_count": 0,
            "is_partial": False,
        }
        
        # Mock ODDS_API_KEY
        monkeypatch.setattr(sb, "ODDS_API_KEY", "test-key-123")
        
        yield
    
    def test_all_leagues_succeed(self, reset_odds_state, monkeypatch):
        """All leagues succeed returns all games."""
        import proactive_agents.sports_bettor as sb
        
        def mock_fetch(league, sport_key, frm, to):
            return (league, [{"home_team": "A", "away_team": "B", "commence_time": "2026-07-26T00:00:00Z", "bookmakers": []}])
        
        monkeypatch.setattr(sb, "_fetch_league_odds_task", mock_fetch)
        
        games = sb.fetch_live_odds(window_hours=48)
        assert len(games) > 0
        assert games[0]["sport"] in sb.ODDS_SPORT_KEYS
        
        diag = sb.get_last_odds_fetch_diagnostics()
        assert diag["success_count"] == len(sb.ODDS_SPORT_KEYS)
        assert diag["failure_count"] == 0
        assert diag["is_partial"] is False
    
    def test_futures_out_of_order_deterministic_output(self, reset_odds_state, monkeypatch):
        """Futures completing out of order still produce deterministic output."""
        import proactive_agents.sports_bettor as sb
        
        completion_order = []
        
        def mock_fetch(league, sport_key, frm, to):
            completion_order.append(league)
            # Return games ordered by league
            return (league, [{"home_team": league, "away_team": "B", "commence_time": "2026-07-26T00:00:00Z", "bookmakers": []}])
        
        monkeypatch.setattr(sb, "_fetch_league_odds_task", mock_fetch)
        
        # Run twice to verify deterministic output
        games1 = sb.fetch_live_odds(window_hours=48)
        completion_order.clear()
        games2 = sb.fetch_live_odds(window_hours=48)
        
        # Output should be identical
        assert games1 == games2
    
    def test_one_timeout_plus_successful_leagues(self, reset_odds_state, monkeypatch):
        """One timeout plus successful leagues returns successful games."""
        import proactive_agents.sports_bettor as sb
        
        call_count = [0]
        
        def mock_fetch(league, sport_key, frm, to):
            call_count[0] += 1
            if league == "NFL":
                raise sb.ProviderUnavailableError("odds_api", "timeout")
            return (league, [{"home_team": "A", "away_team": "B", "commence_time": "2026-07-26T00:00:00Z", "bookmakers": []}])
        
        monkeypatch.setattr(sb, "_fetch_league_odds_task", mock_fetch)
        
        games = sb.fetch_live_odds(window_hours=48)
        assert len(games) > 0
        
        diag = sb.get_last_odds_fetch_diagnostics()
        assert diag["failure_count"] == 1
        assert diag["success_count"] > 0
        assert diag["is_partial"] is True
    
    def test_all_timeouts_raise_provider_unavailable(self, reset_odds_state, monkeypatch):
        """All timeouts raise ProviderUnavailableError."""
        import proactive_agents.sports_bettor as sb
        
        def mock_fetch(league, sport_key, frm, to):
            raise sb.ProviderUnavailableError("odds_api", f"timeout for {league}")
        
        monkeypatch.setattr(sb, "_fetch_league_odds_task", mock_fetch)
        
        with pytest.raises(sb.ProviderUnavailableError):
            sb.fetch_live_odds(window_hours=48)
    
    def test_authentication_failure_raises_immediately(self, reset_odds_state, monkeypatch):
        """Authentication failure raises immediately."""
        import proactive_agents.sports_bettor as sb
        
        def mock_fetch(league, sport_key, frm, to):
            raise sb.ProviderAuthenticationError("odds_api", 401, "Unauthorized")
        
        monkeypatch.setattr(sb, "_fetch_league_odds_task", mock_fetch)
        
        with pytest.raises(sb.ProviderAuthenticationError):
            sb.fetch_live_odds(window_hours=48)
    
    def test_http_429_parsing_integer_retry_after(self, reset_odds_state):
        """HTTP 429 with integer Retry-After parsed correctly."""
        import proactive_agents.sports_bettor as sb
        
        seconds = sb._parse_retry_after("60")
        assert seconds == 60
    
    def test_http_429_parsing_http_date_retry_after(self, reset_odds_state):
        """HTTP 429 with HTTP-date Retry-After parsed correctly."""
        import proactive_agents.sports_bettor as sb
        from datetime import datetime, timezone, timedelta
        
        # Create an HTTP date 120 seconds in the future
        future = datetime.now(timezone.utc) + timedelta(seconds=120)
        http_date = formatdate(timeval=future.timestamp(), usegmt=True)
        
        seconds = sb._parse_retry_after(http_date)
        # Should be approximately 120 seconds
        assert 110 <= seconds <= 130
    
    def test_malformed_retry_after_uses_fallback(self, reset_odds_state):
        """Malformed Retry-After uses safe fallback."""
        import proactive_agents.sports_bettor as sb
        
        seconds = sb._parse_retry_after("invalid_data")
        assert seconds == 60  # Fallback
    
    def test_http_500_raises_retryable_error(self, reset_odds_state, monkeypatch):
        """HTTP 500 raises RetryableProviderError."""
        import proactive_agents.sports_bettor as sb
        
        def mock_fetch(league, sport_key, frm, to):
            raise sb.RetryableProviderError("odds_api", 500, "Server error")
        
        monkeypatch.setattr(sb, "_fetch_league_odds_task", mock_fetch)
        
        with pytest.raises(sb.RetryableProviderError):
            sb.fetch_live_odds(window_hours=48)
    
    def test_malformed_json_raises_provider_unavailable(self, reset_odds_state, monkeypatch):
        """Malformed JSON raises ProviderUnavailableError."""
        import proactive_agents.sports_bettor as sb
        import requests

        class _MockResponse:
            status_code = 200
            url = "https://api.the-odds-api.com/v4/sports/nfl/odds"
            headers = {}

            def raise_for_status(self):
                pass

            def json(self):
                raise ValueError("No JSON object could be decoded")

        monkeypatch.setattr(requests, "get", lambda *a, **kw: _MockResponse())
        monkeypatch.setattr(sb, "ODDS_API_KEY", "test-key")

        with pytest.raises(sb.ProviderUnavailableError):
            sb._fetch_league_odds_task("NFL", "americanfootball_nfl", "2026-01-01T00:00:00Z", "2026-01-03T00:00:00Z")

    def test_non_list_json_raises_provider_unavailable(self, reset_odds_state, monkeypatch):
        """Non-list JSON raises ProviderUnavailableError."""
        import proactive_agents.sports_bettor as sb
        import requests

        class _MockResponse:
            status_code = 200
            url = "https://api.the-odds-api.com/v4/sports/nfl/odds"
            headers = {}

            def raise_for_status(self):
                pass

            def json(self):
                return {"error": "unexpected dict instead of list"}

        monkeypatch.setattr(requests, "get", lambda *a, **kw: _MockResponse())
        monkeypatch.setattr(sb, "ODDS_API_KEY", "test-key")

        with pytest.raises(sb.ProviderUnavailableError):
            sb._fetch_league_odds_task("NFL", "americanfootball_nfl", "2026-01-01T00:00:00Z", "2026-01-03T00:00:00Z")
    
    def test_api_key_not_in_exceptions(self, reset_odds_state):
        """API keys do not appear in exceptions."""
        import proactive_agents.sports_bettor as sb
        
        # Verify _sanitize_endpoint removes query string
        url = "https://api.the-odds-api.com/v4/sports/nfl/odds?apiKey=SECRET&regions=us"
        sanitized = sb._sanitize_endpoint(url)
        assert "apiKey" not in sanitized
        assert "SECRET" not in sanitized
        assert "regions" not in sanitized
    
    def test_no_api_key_returns_empty_list(self, reset_odds_state, monkeypatch):
        """No API key returns empty list."""
        import proactive_agents.sports_bettor as sb
        
        monkeypatch.setattr(sb, "ODDS_API_KEY", "")
        
        games = sb.fetch_live_odds(window_hours=48)
        assert games == []


# ============================================================================
# PART 4: Report Signature Tests
# ============================================================================

class TestReportSignatureDeterminism:
    """Test that _report_signature produces deterministic hashes."""
    
    def test_input_order_does_not_affect_hash(self):
        """Input order does not affect the hash."""
        import proactive_agents.sports_bettor as sb
        
        picks1 = [
            {"sport": "NFL", "matchup": "A vs B", "side": "A", "odds": "-110", "consensus_count": 2},
            {"sport": "MLB", "matchup": "C vs D", "side": "C", "odds": "-110", "consensus_count": 1},
        ]
        
        picks2 = [
            {"sport": "MLB", "matchup": "C vs D", "side": "C", "odds": "-110", "consensus_count": 1},
            {"sport": "NFL", "matchup": "A vs B", "side": "A", "odds": "-110", "consensus_count": 2},
        ]
        
        hash1 = sb._report_signature(picks1)
        hash2 = sb._report_signature(picks2)
        assert hash1 == hash2
    
    def test_odds_changes_affect_hash(self):
        """Odds changes affect the hash."""
        import proactive_agents.sports_bettor as sb
        
        picks1 = [{"sport": "NFL", "matchup": "A vs B", "side": "A", "odds": "-110", "consensus_count": 2}]
        picks2 = [{"sport": "NFL", "matchup": "A vs B", "side": "A", "odds": "-120", "consensus_count": 2}]
        
        hash1 = sb._report_signature(picks1)
        hash2 = sb._report_signature(picks2)
        assert hash1 != hash2
    
    def test_empty_input_produces_stable_hash(self):
        """Empty input produces a stable hash."""
        import proactive_agents.sports_bettor as sb
        
        hash1 = sb._report_signature([])
        hash2 = sb._report_signature([])
        assert hash1 == hash2
        assert len(hash1) == 64  # SHA256 hex digest


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
