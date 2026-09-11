"""Sharp Picks must not text a game that has already been played.

2026-09-11, 9:00 AM CT: the Odds API answered HTTP 502, so the sweep ran with
no slate. Grok read Thursday night's posts, tagged them game_day "today", and
Henry was texted "LA Rams -3.5" and "Florida A&M @ Miami OVER 62.5" as today's
plays. Both games had gone final twelve hours earlier (27-7 and 77-7). Nothing
in the pipeline had ever asked whether a game was still ahead: the slate
answered that for free on healthy days by listing only games ahead, and the
one day it was missing, so was the check.

Nothing here touches the network.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ivy_core import espn_odds, text_delivery
from ivy_core.pipeline_status import PipelineStatus, RetryableProviderError
from proactive_agents import sports_bettor

NOW = datetime(2026, 9, 11, 14, 0, tzinfo=timezone.utc)  # Fri Sep 11, 9:00 AM CT

RAMS_FINAL = {"away_team": "San Francisco 49ers", "home_team": "Los Angeles Rams",
              "start": "2026-09-11T00:35Z", "state": "post", "status": "STATUS_FINAL"}
MIAMI_FINAL = {"away_team": "Florida A&M Rattlers", "home_team": "Miami Hurricanes",
               "start": "2026-09-11T00:00Z", "state": "post", "status": "STATUS_FINAL"}
RAIDERS_AHEAD = {"away_team": "Las Vegas Raiders", "home_team": "Miami Dolphins",
                 "start": "2026-09-13T20:25Z", "state": "pre", "status": "STATUS_SCHEDULED"}
SCHEDULES = {"nfl": [RAMS_FINAL, RAIDERS_AHEAD], "ncaaf": [MIAMI_FINAL]}

# What Grok returned that morning, verbatim in shape: tagged today, no start.
THURSDAY_POSTS = [
    {"sport": "NFL", "matchup": "SF 49ers @ LA Rams", "side": "LA Rams -3.5", "odds": "-112",
     "handicapper": "sharp1", "confidence": "high", "game_day": "today", "start_time": None,
     "reasoning": "Rams at home."},
    {"sport": "NFL", "matchup": "SF 49ers @ LA Rams", "side": "LA Rams -3.5", "odds": "-112",
     "handicapper": "sharp2", "confidence": None, "game_day": "today", "start_time": None,
     "reasoning": "Fade SF."},
    {"sport": "NCAAF", "matchup": "Florida A&M @ Miami", "side": "OVER 62.5", "odds": "-115",
     "handicapper": "sharp3", "confidence": "medium", "game_day": "today", "start_time": None,
     "reasoning": "Miami scores."},
]


def pick(**over):
    base = {"sport": "NFL", "matchup": "SF 49ers @ LA Rams", "side": "LA Rams -3.5",
            "odds": "-112", "handicappers": ["sharp1"], "consensus_count": 1,
            "is_consensus": False, "game_day": "today", "start": None}
    base.update(over)
    return base


@pytest.fixture
def espn(monkeypatch):
    """ESPN answers from a canned schedule and records which sports it was asked about."""
    asked = []

    def fetch_schedule(sport, now=None):
        asked.append(sport)
        return list(SCHEDULES.get(sport, []))

    monkeypatch.setattr(espn_odds, "fetch_schedule", fetch_schedule)
    monkeypatch.setattr(espn_odds, "fetch_markets", lambda sport: [])
    return asked


class TestTheGate:
    def test_the_regression_a_final_game_tagged_today_is_dropped(self, espn):
        kept, dropped = sports_bettor.drop_stale_picks([pick()], now=NOW)
        assert kept == []
        assert dropped[0]["stale_reason"] == "already played (Thu Sep 10, 7:35 PM CT)"
        assert dropped[0]["start"] == "2026-09-11T00:35Z", "ESPN's start is stamped for the record"

    def test_college_football_is_checked_too(self, espn):
        """The slate never carries NCAAF (no Odds API key for it), so it is the
        sport most exposed — the check cannot be 'is it on the slate'."""
        kept, dropped = sports_bettor.drop_stale_picks(
            [pick(sport="NCAAF", matchup="Florida A&M @ Miami", side="OVER 62.5")], now=NOW)
        assert kept == []
        assert "already played" in dropped[0]["stale_reason"]

    def test_a_game_still_ahead_is_kept_with_espns_start(self, espn):
        kept, dropped = sports_bettor.drop_stale_picks(
            [pick(matchup="Raiders @ Dolphins", side="Dolphins -3")], now=NOW)
        assert dropped == []
        assert kept[0]["start"] == "2026-09-13T20:25Z"
        assert kept[0]["start_source"] == "espn"

    def test_a_game_in_progress_is_not_a_pregame_play(self, espn, monkeypatch):
        live = dict(RAIDERS_AHEAD, state="in", status="STATUS_IN_PROGRESS")
        monkeypatch.setattr(espn_odds, "fetch_schedule", lambda sport, now=None: [live])
        kept, dropped = sports_bettor.drop_stale_picks([pick(matchup="Raiders @ Dolphins")], now=NOW)
        assert kept == []
        assert dropped[0]["stale_reason"] == "in progress"

    def test_a_slate_verified_pick_never_asks_espn(self, espn):
        kept, dropped = sports_bettor.drop_stale_picks(
            [pick(start="2026-09-11T00:35Z", start_source="slate")], now=NOW)
        assert kept and not dropped
        assert espn == [], "the slate vouched for it; ESPN was not consulted"

    def test_each_sport_is_fetched_once(self, espn):
        board = [pick(matchup="Raiders @ Dolphins"),
                 pick(matchup="Raiders @ Dolphins", side="Over 44"),
                 pick(sport="NCAAF", matchup="Florida A&M @ Miami")]
        sports_bettor.drop_stale_picks(board, now=NOW)
        assert espn == ["nfl", "ncaaf"]

    # Sports ESPN does not list fall back to what the handicapper said.
    def test_an_unlisted_sport_with_a_future_start_is_kept(self, espn):
        kept, dropped = sports_bettor.drop_stale_picks(
            [pick(sport="KBO", matchup="LG Twins @ Doosan Bears", side="LG Twins ML",
                  start="2026-09-12T09:30:00+09:00")], now=NOW)
        assert dropped == []
        assert kept[0]["start_source"] == "post"

    def test_an_unlisted_sport_with_a_past_start_is_dropped(self, espn):
        kept, dropped = sports_bettor.drop_stale_picks(
            [pick(sport="KBO", matchup="LG Twins @ Doosan Bears",
                  start="2026-09-11T09:30:00+09:00")], now=NOW)
        assert kept == []
        assert dropped[0]["stale_reason"] == "started Thu Sep 10, 7:30 PM CT"

    def test_no_start_from_anywhere_is_not_texted_as_today(self, espn):
        """The exact shape of the two picks that went out: game_day 'today',
        start null, no slate. 'today' is Grok's reading of a post's age."""
        kept, dropped = sports_bettor.drop_stale_picks(
            [pick(sport="KBO", matchup="LG Twins @ Doosan Bears", start=None, game_day="today")],
            now=NOW)
        assert kept == []
        assert dropped[0]["stale_reason"] == "no start time from the slate, ESPN or the post"

    def test_a_bare_date_means_the_whole_day(self, espn):
        today, yesterday = [pick(sport="KBO", matchup="LG Twins @ Doosan Bears", start=d)
                            for d in ("2026-09-11", "2026-09-10")]
        kept, dropped = sports_bettor.drop_stale_picks([today, yesterday], now=NOW)
        assert [e["start"] for e in kept] == ["2026-09-11"]
        assert [e["stale_reason"] for e in dropped] == ["started Thu Sep 10"]

    def test_espn_being_down_falls_through_to_the_post(self, espn, monkeypatch):
        def boom(sport, now=None):
            raise RuntimeError("espn down")

        monkeypatch.setattr(espn_odds, "fetch_schedule", boom)
        ahead = pick(matchup="Raiders @ Dolphins", start="2026-09-13T20:25:00Z")
        stale = pick()
        kept, dropped = sports_bettor.drop_stale_picks([ahead, stale], now=NOW)
        assert kept == [ahead]
        assert dropped == [stale]

    def test_an_empty_board_asks_nothing(self, espn):
        assert sports_bettor.drop_stale_picks([], now=NOW) == ([], [])
        assert espn == []


class TestRenderingAStart:
    def test_a_bare_date_is_not_the_evening_before(self):
        """"2026-09-11" used to print as "Thu Sep 10, 7:00 PM CT"."""
        assert sports_bettor._fmt_start("2026-09-11") == "Fri Sep 11"

    def test_a_full_timestamp_is_central_time(self):
        assert sports_bettor._fmt_start("2026-09-11T00:35Z") == "Thu Sep 10, 7:35 PM CT"


class TestThePromptKnowsWhatDayItIs:
    def test_the_date_and_the_window_are_stated(self):
        prompt = sports_bettor._build_sweep_prompt(
            ["h"], sports_bettor._slate_clause(""), sports_bettor.SPORT_HINTS, now=NOW)
        assert "It is now Friday, Sep 11, 2026, 9:00 AM Central time (2026-09-11T14:00:00Z)" in prompt
        assert "between now and Sunday, Sep 13, 9:00 AM Central" in prompt
        assert "a handicapper's recap of a game that is over is not a pick" in prompt
        assert "No slate is available" in prompt, "the empty-slate clause still rides along"

    def test_the_example_start_is_in_the_window_not_a_stale_literal(self):
        prompt = sports_bettor._build_sweep_prompt(["h"], "", sports_bettor.SPORT_HINTS, now=NOW)
        assert "e.g. '2026-09-11T17:00:00-05:00'" in prompt

    def test_game_day_is_relative_to_the_stated_date(self):
        prompt = sports_bettor._build_sweep_prompt(["h"], "", sports_bettor.SPORT_HINTS, now=NOW)
        assert "relative to the Central date given above" in prompt

    def test_the_clock_defaults_to_now(self):
        prompt = sports_bettor._build_sweep_prompt(["h"], "", sports_bettor.SPORT_HINTS)
        assert "It is now " in prompt
        assert f", {datetime.now(timezone.utc).year}, " in prompt


def _run(monkeypatch, tmp_path, *, raw_picks, last=None, force=True, send=True):
    """Drive run() with the slate down (HTTP 502), a canned sweep, and every
    delivery hook captured. Returns (result, notices, texts, saved_picks, saved_reports):
    notices are texts sent straight through send_imessage, texts are reports
    that went through deliver_report."""
    monkeypatch.setattr(sports_bettor._outbox, "OUTBOX_DIR", tmp_path / "outbox")

    def no_slate():
        raise RetryableProviderError("The Odds API", 502, "Odds API server error (HTTP 502)")

    monkeypatch.setattr(sports_bettor, "fetch_live_odds", no_slate)
    monkeypatch.setattr(sports_bettor, "sweep_with_retry", lambda games: [dict(p) for p in raw_picks])
    monkeypatch.setattr(sports_bettor, "repair_matchups", lambda picks, games: (picks, []))
    monkeypatch.setattr(sports_bettor, "enrich_picks", lambda merged, games: None)
    monkeypatch.setattr(sports_bettor, "format_picks_pdf", lambda merged: None)
    monkeypatch.setattr(sports_bettor, "load_last_report", lambda: dict(last or {}))
    notices, texts, saved_picks, saved_reports = [], [], [], []
    monkeypatch.setattr(sports_bettor, "save_last_report",
                        lambda sig, msg: saved_reports.append((sig, msg)))
    monkeypatch.setattr(sports_bettor, "save_picks",
                        lambda picks, report_date=None: saved_picks.extend(picks))
    monkeypatch.setattr(sports_bettor, "send_imessage",
                        lambda phone, body: notices.append(body) or True)
    monkeypatch.setattr(text_delivery, "send_imessage",
                        lambda phone, body: texts.append(body) or True)
    monkeypatch.setattr(text_delivery, "send_imessage_attachment", lambda *a, **k: None)
    result = sports_bettor.run(force=force, send=send)
    return result, notices, texts, saved_picks, saved_reports


class TestTheRun:
    def test_the_stale_board_is_never_texted_as_plays_or_saved(self, espn, monkeypatch, tmp_path, capsys):
        result, notices, texts, saved_picks, saved_reports = _run(
            monkeypatch, tmp_path, raw_picks=THURSDAY_POSTS)

        assert saved_picks == [], "nothing already played reaches picks.db"
        assert texts == [], "no report went out"
        assert result["status"] == PipelineStatus.NO_QUALIFYING_PICKS.value
        assert result["picks"] == 0
        assert result["sent"] is True, "but Henry heard what happened"

        assert len(notices) == 1
        notice = notices[0]
        assert "nothing bettable" in notice
        assert "SF 49ers @ LA Rams" in notice
        assert "already played (Thu Sep 10, 7:35 PM CT)" in notice
        assert "Florida A&M @ Miami" in notice
        assert "schedule feed was down" in notice, "the cause is named"
        assert "Today" not in notice
        assert saved_reports and saved_reports[0][1] == notice, "deduped like any other board"

        out = capsys.readouterr().out
        assert "Dropped NFL SF 49ers @ LA Rams" in out
        assert "Dropped NCAAF Florida A&M @ Miami" in out

    def test_a_live_pick_still_goes_out_without_the_stale_ones(self, espn, monkeypatch, tmp_path):
        ahead = [dict(p, matchup="Raiders @ Dolphins", side="Dolphins -3", odds="-110")
                 for p in THURSDAY_POSTS[:2]]
        result, notices, texts, saved_picks, saved_reports = _run(
            monkeypatch, tmp_path, raw_picks=THURSDAY_POSTS + ahead)
        joined = "\n".join(texts)

        assert notices == [], "no already-played notice when there is a board to send"
        assert "Dolphins -3" in joined
        assert "LA Rams -3.5" not in joined
        assert "OVER 62.5" not in joined
        assert "Sun Sep 13, 3:25 PM CT" in joined, "the when is ESPN's, not Grok's 'Today'"
        assert "Today" not in joined
        assert [p["side"] for p in saved_picks] == ["Dolphins -3"]
        assert result["picks"] == 1
        assert result["sent"] is True

    def test_the_same_stale_board_does_not_nag_on_the_next_scheduled_run(self, espn, monkeypatch, tmp_path):
        _, _, _, _, saved_reports = _run(monkeypatch, tmp_path, raw_picks=THURSDAY_POSTS)
        signature = saved_reports[0][0]

        result, notices, texts, _, _ = _run(
            monkeypatch, tmp_path, raw_picks=THURSDAY_POSTS,
            last={"signature": signature}, force=False)

        assert notices == [] and texts == []
        assert result["status"] == PipelineStatus.NO_QUALIFYING_PICKS.value
        assert result["sent"] is False

    def test_an_ad_hoc_request_always_hears_back(self, espn, monkeypatch, tmp_path):
        _, _, _, _, saved_reports = _run(monkeypatch, tmp_path, raw_picks=THURSDAY_POSTS)
        result, notices, _, _, _ = _run(
            monkeypatch, tmp_path, raw_picks=THURSDAY_POSTS,
            last={"signature": saved_reports[0][0]}, force=True)
        assert len(notices) == 1
        assert result["sent"] is True

    def test_a_dry_run_sends_nothing(self, espn, monkeypatch, tmp_path):
        result, notices, texts, saved_picks, saved_reports = _run(
            monkeypatch, tmp_path, raw_picks=THURSDAY_POSTS, send=False)
        assert notices == [] and texts == [] and saved_reports == [] and saved_picks == []
        assert result["status"] == PipelineStatus.NO_QUALIFYING_PICKS.value
