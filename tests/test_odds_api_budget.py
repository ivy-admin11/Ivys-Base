"""The Odds API went dark for seven weeks, and neither the cause nor the cost
was visible from the alert it sent.

Two separate faults, both covered here:

1. A 401 from this provider means either "your key is wrong" or "your key is
   fine and your allowance is gone". They need opposite fixes, and the alert
   reported both as the first one — so the operator rotated a key that was
   already valid while the real problem sat untouched.

2. Nothing needed the prices. Validation reads teams and start times, and
   pricing has been off since ENABLE_PICK_PRICING was set False. But the slate
   was still pulled from /odds, which bills a credit per market per region:
   54 a run, 162 a day, against 500 a month. The grading sweep was worse — it
   asked every sport on the platform for scores four times a day even when
   nothing at all was waiting to be graded.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ivy_core.pipeline_status import ProviderAuthenticationError  # noqa: E402


class _Resp:
    """Minimal stand-in for a requests.Response."""

    def __init__(self, status_code=200, payload=None, text="", url="https://api.example/x"):
        self.status_code = status_code
        self._payload = payload
        self.text = text or ""
        self.url = url

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError("raise_for_status should not be reached for handled codes")


class TestA401SaysWhichKindItIs:
    def test_exhausted_credits_are_not_reported_as_a_bad_key(self):
        e = ProviderAuthenticationError(
            provider="odds_api", status_code=401, message="m",
            error_code="OUT_OF_USAGE_CREDITS",
        )
        assert e.is_quota_exhausted

    def test_a_genuinely_invalid_key_is_not_mistaken_for_quota(self):
        e = ProviderAuthenticationError(
            provider="odds_api", status_code=401, message="m", error_code="INVALID_KEY",
        )
        assert not e.is_quota_exhausted

    def test_an_absent_code_does_not_claim_quota(self):
        """Silence must not be read as the more reassuring diagnosis."""
        e = ProviderAuthenticationError(provider="odds_api", status_code=401, message="m")
        assert not e.is_quota_exhausted

    def test_the_raised_error_carries_the_providers_own_code(self, monkeypatch):
        import proactive_agents.sports_bettor as sb

        body = {"message": "Usage quota has been reached.",
                "error_code": "OUT_OF_USAGE_CREDITS"}
        monkeypatch.setattr(sb, "ODDS_API_KEY", "k" * 32)
        monkeypatch.setattr(sb, "discover_sport_keys", lambda: [("MLB", "baseball_mlb")])
        monkeypatch.setattr(sb.requests, "get",
                            lambda *a, **k: _Resp(401, body, str(body)))

        with pytest.raises(ProviderAuthenticationError) as ei:
            sb.fetch_live_odds()

        assert ei.value.error_code == "OUT_OF_USAGE_CREDITS"
        assert ei.value.is_quota_exhausted
        assert "credits are used up" in ei.value.message

    def test_a_bad_key_still_reads_as_a_bad_key(self, monkeypatch):
        import proactive_agents.sports_bettor as sb

        body = {"message": "API key is not valid.", "error_code": "INVALID_KEY"}
        monkeypatch.setattr(sb, "ODDS_API_KEY", "k" * 32)
        monkeypatch.setattr(sb, "discover_sport_keys", lambda: [("MLB", "baseball_mlb")])
        monkeypatch.setattr(sb.requests, "get",
                            lambda *a, **k: _Resp(401, body, str(body)))

        with pytest.raises(ProviderAuthenticationError) as ei:
            sb.fetch_live_odds()

        assert not ei.value.is_quota_exhausted
        assert "credentials were rejected" in ei.value.message


class TestTheSlateIsNotBought:
    """Validation needs the fixtures, not the prices, and /events is free."""

    def _capture(self, monkeypatch, pricing):
        import proactive_agents.sports_bettor as sb
        seen = {}

        def fake_get(url, params=None, timeout=None):
            seen["url"] = url
            seen["params"] = params or {}
            return _Resp(200, [])

        monkeypatch.setattr(sb, "ODDS_API_KEY", "k" * 32)
        monkeypatch.setattr(sb, "ENABLE_PICK_PRICING", pricing)
        monkeypatch.setattr(sb, "discover_sport_keys", lambda: [("MLB", "baseball_mlb")])
        monkeypatch.setattr(sb.requests, "get", fake_get)
        sb.fetch_live_odds()
        return seen

    def test_with_pricing_off_it_uses_the_unbilled_events_endpoint(self, monkeypatch):
        seen = self._capture(monkeypatch, pricing=False)
        assert seen["url"].endswith("/events")
        assert "markets" not in seen["params"], "markets is what the credit is billed for"
        assert "regions" not in seen["params"]

    def test_the_time_window_survives_the_switch(self, monkeypatch):
        """Free must not mean unfiltered — a whole season is not the slate."""
        seen = self._capture(monkeypatch, pricing=False)
        assert seen["params"].get("commenceTimeFrom")
        assert seen["params"].get("commenceTimeTo")

    def test_the_pricing_flag_can_no_longer_reach_the_paid_endpoint(self, monkeypatch):
        """Written on 2026-09-09, inverted on 2026-09-10.

        For one day the endpoint followed ENABLE_PICK_PRICING, so turning
        pricing on would silently restore a 54-credit-per-run call and walk
        back into the exhaustion that took validation down for seven weeks.
        Prices come from ESPN now — free, and a different feed entirely — so
        this feed is only ever the free schedule, whatever the flag says.
        """
        for pricing in (True, False):
            seen = self._capture(monkeypatch, pricing=pricing)
            assert seen["url"].endswith("/events"), f"pricing={pricing} reached a paid endpoint"
            assert "markets" not in seen["params"]
            assert "regions" not in seen["params"]


class TestGradingOnlyPaysForWhatItNeeds:
    @pytest.fixture
    def picks_db(self, tmp_path, monkeypatch):
        db = tmp_path / "picks.db"
        con = sqlite3.connect(db)
        con.executescript(
            "CREATE TABLE picks (id INTEGER PRIMARY KEY, sport TEXT, matchup TEXT,"
            " side TEXT, game_day TEXT, sharp_count INT, handicapper TEXT,"
            " report_date TEXT, created_at TEXT);"
            "CREATE TABLE results (pick_id INT, result TEXT);"
        )
        con.commit()
        con.close()
        import ivy_core.result_updater as ru
        monkeypatch.setattr(ru, "PICKS_DB", db)
        return db

    def test_nothing_pending_means_no_request_at_all(self, picks_db, monkeypatch):
        """The sweep used to spend 80 credits to discover it had no work."""
        import ivy_core.result_updater as ru
        calls = []
        monkeypatch.setattr(ru.requests, "get",
                            lambda *a, **k: calls.append(a) or _Resp(200, []))
        out = ru.auto_update_results()
        assert out == {"updated": 0, "pending": 0}
        assert calls == [], "an empty queue must not touch the paid API"

    def test_only_the_sports_with_pending_picks_are_queried(self, picks_db, monkeypatch):
        import ivy_core.result_updater as ru
        con = sqlite3.connect(picks_db)
        con.execute("INSERT INTO picks (sport, matchup, side, game_day, sharp_count,"
                    " handicapper, report_date, created_at) VALUES"
                    " ('MLB','A @ B','B ML','2026-09-08',2,'x','2026-09-08','2026-09-08')")
        con.commit()
        con.close()

        asked = []
        sports = [{"key": "baseball_mlb", "group": "Baseball", "title": "MLB"},
                  {"key": "soccer_epl", "group": "Soccer", "title": "EPL"},
                  {"key": "icehockey_nhl", "group": "Hockey", "title": "NHL"}]

        def fake_get(url, params=None, timeout=None):
            if url.endswith("/sports"):
                return _Resp(200, sports)
            asked.append(url)
            return _Resp(200, [])

        monkeypatch.setattr(ru.requests, "get", fake_get)
        monkeypatch.setattr(ru, "_get_odds_api_key", lambda: "k" * 32)
        ru.auto_update_results()

        assert any("baseball_mlb" in u for u in asked), "the sport with a pick must be graded"
        assert not any("soccer_epl" in u or "icehockey_nhl" in u for u in asked), \
            "sports with nothing pending must not be billed for"


class TestPlaceholdersNeverReachTheDatabase:
    """Eight rows reading "A vs B" with side "A" landed in the live picks table
    on 2026-09-01 — the model echoing the prompt's own example back, during the
    weeks when the exhausted Odds API could not validate anything against a
    real slate. A row naming no real teams can never be graded or reported, so
    it is only ever noise in the one table that is supposed to be evidence."""

    def test_template_text_is_refused(self):
        from ivy_core.picks_tracker import _is_placeholder_matchup
        for junk in ("A vs B", "a  vs  b", "Team A vs Team B", "A @ B", "X vs Y"):
            assert _is_placeholder_matchup(junk), junk

    def test_a_real_matchup_is_kept(self):
        """Over-eager filtering would silently drop real picks, which is worse
        than the noise it is cleaning up."""
        from ivy_core.picks_tracker import _is_placeholder_matchup
        for real in ("Astros vs Rangers", "Arizona Diamondbacks @ Houston Astros",
                     "Atlanta vs Baltimore", "A's vs Blue Jays"):
            assert not _is_placeholder_matchup(real), real

    def test_missing_matchups_are_left_to_the_existing_not_null_guard(self):
        from ivy_core.picks_tracker import _is_placeholder_matchup
        assert not _is_placeholder_matchup(None)
        assert not _is_placeholder_matchup("")
