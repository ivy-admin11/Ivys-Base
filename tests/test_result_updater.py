"""ivy_core.result_updater: score matching, spread/total/moneyline math,
auth-failure handling, and batched reconciliation.

Every test mocks requests/picks_tracker/picks_sync — none of these ever
hit the real Odds API, database, or Google Sheets.
"""

from unittest.mock import MagicMock

import pytest

import config
from ivy_core import result_updater as ru


def _game(home, away, home_score, away_score, scores_order="home_first", **extra):
    scores = [{"name": home, "score": home_score}, {"name": away, "score": away_score}]
    if scores_order == "away_first":
        scores = list(reversed(scores))
    game = {"home_team": home, "away_team": away, "scores": scores, "completed": True}
    game.update(extra)
    return game


def test_score_array_order_does_not_affect_moneyline_outcome():
    game_home_first = _game("Kansas City Chiefs", "Buffalo Bills", 24, 20, scores_order="home_first")
    game_away_first = _game("Kansas City Chiefs", "Buffalo Bills", 24, 20, scores_order="away_first")

    result_a = ru._extract_moneyline_result(game_home_first, "Kansas City Chiefs ML")
    result_b = ru._extract_moneyline_result(game_away_first, "Kansas City Chiefs ML")

    assert result_a == result_b == "W"


def test_multiword_team_name_moneyline():
    game = _game("Kansas City Chiefs", "Buffalo Bills", 24, 20)
    assert ru._extract_moneyline_result(game, "Kansas City Chiefs ML") == "W"
    assert ru._extract_moneyline_result(game, "Buffalo Bills ML") == "L"


def test_moneyline_tie_is_a_push():
    game = _game("Kansas City Chiefs", "Buffalo Bills", 20, 20)
    assert ru._extract_moneyline_result(game, "Kansas City Chiefs ML") == "P"


def test_spread_positive_and_negative_home_and_away():
    game = _game("Kansas City Chiefs", "Buffalo Bills", 24, 20)  # home wins by 4
    # Home favored by -3: adjusted = 24 - 3 = 21 > 20 -> W
    assert ru._extract_spread_result(game, "Kansas City Chiefs -3") == "W"
    # Home favored by -5: adjusted = 24 - 5 = 19 < 20 -> L
    assert ru._extract_spread_result(game, "Kansas City Chiefs -5") == "L"
    # Away getting +3: adjusted = 20 + 3 = 23 < 24 -> L
    assert ru._extract_spread_result(game, "Buffalo Bills +3") == "L"
    # Away getting +7: adjusted = 20 + 7 = 27 > 24 -> W
    assert ru._extract_spread_result(game, "Buffalo Bills +7") == "W"
    # Exact push
    assert ru._extract_spread_result(game, "Kansas City Chiefs -4") == "P"


def test_totals_over_under_and_push():
    game = _game("Kansas City Chiefs", "Buffalo Bills", 24, 20)  # total = 44
    assert ru._extract_over_under_result(game, "Over 40.5") == "W"
    assert ru._extract_over_under_result(game, "Under 40.5") == "L"
    assert ru._extract_over_under_result(game, "Over 44") == "P"
    assert ru._extract_over_under_result(game, "Under 44") == "P"


def test_matchup_normalization_matches_multiword_teams_with_apostrophes():
    assert ru._match_teams_in_matchup(
        "Kansas City Chiefs @ Buffalo Bills", "Kansas City Chiefs", "Buffalo Bills"
    )
    assert not ru._match_teams_in_matchup(
        "Dallas Cowboys @ New York Giants", "Kansas City Chiefs", "Buffalo Bills"
    )


def test_auth_failure_distinguished_from_legitimate_empty_result(monkeypatch):
    resp_401 = MagicMock(status_code=401)
    monkeypatch.setattr(ru.requests, "get", lambda *a, **k: resp_401)
    monkeypatch.setattr(config, "ODDS_API_KEY", "fake-key")

    with pytest.raises(ru.OddsApiAuthenticationError):
        ru._request_odds_api("/sports", {})


def test_empty_result_set_is_not_an_error(monkeypatch):
    resp_ok = MagicMock(status_code=200)
    resp_ok.json.return_value = []
    monkeypatch.setattr(ru.requests, "get", lambda *a, **k: resp_ok)
    monkeypatch.setattr(config, "ODDS_API_KEY", "fake-key")

    result = ru._request_odds_api("/sports", {})
    assert result == []


def test_missing_api_key_returns_none_without_a_request(monkeypatch):
    called = []
    monkeypatch.setattr(ru.requests, "get", lambda *a, **k: called.append(1))
    monkeypatch.setattr(config, "ODDS_API_KEY", "")

    result = ru._request_odds_api("/sports", {})
    assert result is None
    assert not called


def test_auto_update_results_batches_updates_and_syncs_sheet_once(monkeypatch):
    game = _game("Kansas City Chiefs", "Buffalo Bills", 24, 20)
    game["sport_key"] = "americanfootball_nfl"
    monkeypatch.setattr(ru, "get_completed_games", lambda: [game])

    pending = [{
        "id": 1, "sport": "NFL", "matchup": "Kansas City Chiefs @ Buffalo Bills",
        "side": "Kansas City Chiefs ML", "game_day": "today", "sharp_count": 1,
        "handicapper": "sharp1", "report_date": "2026-07-21",
    }]
    monkeypatch.setattr(ru.picks_tracker, "get_pending_picks", lambda: pending)

    batch_calls = []
    monkeypatch.setattr(
        ru.picks_tracker, "batch_update_results",
        lambda updates: batch_calls.append(updates) or len(updates),
    )
    sync_calls = []
    monkeypatch.setattr(
        ru.picks_sync, "sync_canonical_snapshot",
        lambda: sync_calls.append(1) or {"status": "success"},
    )

    result = ru.auto_update_results()

    assert result["updated"] == 1
    assert len(batch_calls) == 1, "all result updates must apply in exactly one batched transaction"
    assert len(sync_calls) == 1, "exactly one Sheet sync per reconciliation batch"
    assert result["sheet_sync"]["status"] == "success"


def test_auto_update_results_skips_sync_when_nothing_changed(monkeypatch):
    monkeypatch.setattr(ru, "get_completed_games", lambda: [])
    monkeypatch.setattr(ru.picks_tracker, "get_pending_picks", lambda: [])
    sync_calls = []
    monkeypatch.setattr(
        ru.picks_sync, "sync_canonical_snapshot",
        lambda: sync_calls.append(1) or {"status": "success"},
    )

    result = ru.auto_update_results()

    assert result["updated"] == 0
    assert not sync_calls, "no Sheet sync should run when nothing changed"
