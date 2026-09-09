"""Consensus detection: two handicappers on one bet must count as two sharps.

The bug these pin: `merge_picks` keyed on the raw matchup and side strings the
sweep wrote, and `repair_matchups` — which rewrites both to the slate's own
team names — ran four lines AFTER it. So "MIL Brewers @ CHC Cubs / MIL Brewers
ML" and "Milwaukee Brewers @ Chicago Cubs / Milwaukee ML" stayed two separate
one-sharp picks. Nothing reached the 2-sharp bar, nothing cleared the quality
filter, and for four days straight (2026-09-06 through 09-08) Ivy sent boards
of 15-18 picks that were all labelled "LOW · 1 sharp" — while five independent
handles were posting them.

The strings below are the real ones from SP-20260908-1501.
"""

import inspect

import pytest

from proactive_agents import sports_bettor as sb


SLATE = [
    {"sport": "MLB", "away": "Milwaukee Brewers", "home": "Chicago Cubs", "commence": None},
    {"sport": "MLB", "away": "Miami Marlins", "home": "Atlanta Braves", "commence": None},
    {"sport": "MLB", "away": "New York Mets", "home": "Miami Marlins", "commence": None},
    {"sport": "MLB", "away": "Toronto Blue Jays", "home": "Athletics", "commence": None},
    {"sport": "MLB", "away": "Los Angeles Angels", "home": "Texas Rangers", "commence": None},
]


def _pipeline(raw, games=SLATE):
    """The real order: canonicalise against the slate, then merge."""
    kept, _dropped = sb.repair_matchups(raw, games)
    return sb.merge_picks(kept)


def _pick(matchup, side, handle, **extra):
    p = {"sport": "MLB", "matchup": matchup, "side": side, "handicapper": handle,
         "odds": None, "confidence": None, "game_day": "today",
         "start_time": None, "reasoning": ""}
    p.update(extra)
    return p


# --------------------------------------------------------------------------
# The regression itself
# --------------------------------------------------------------------------

def test_two_spellings_of_one_moneyline_are_one_bet_with_two_sharps():
    merged = _pipeline([
        _pick("MIL Brewers @ CHC Cubs", "MIL Brewers ML", "cappersforfree"),
        _pick("Milwaukee Brewers @ Chicago Cubs", "Milwaukee ML", "HarryLockPicks"),
    ])
    assert len(merged) == 1, f"should be one bet, got {[e['matchup'] for e in merged]}"
    assert merged[0]["consensus_count"] == 2
    assert merged[0]["is_consensus"] is True
    assert set(merged[0]["handicappers"]) == {"cappersforfree", "HarryLockPicks"}


def test_abbreviation_and_full_name_merge_across_three_handles():
    merged = _pipeline([
        _pick("Toronto Blue Jays @ Athletics", "TOR Blue Jays ML", "PropCaddie"),
        _pick("TOR @ ATH", "Toronto ML", "MassMoneyline"),
        _pick("Toronto Blue Jays @ Athletics", "Blue Jays ML", "billhpicks"),
    ])
    assert len(merged) == 1
    assert merged[0]["consensus_count"] == 3
    grade, count = sb._confidence(merged[0])
    assert (grade, count) == ("HIGH", 3)


def test_merging_before_repair_is_what_failed():
    """Pin the cause, not just the symptom: on the raw strings, no merge."""
    raw = [
        _pick("MIL Brewers @ CHC Cubs", "MIL Brewers ML", "cappersforfree"),
        _pick("Milwaukee Brewers @ Chicago Cubs", "Milwaukee ML", "HarryLockPicks"),
    ]
    wrong_order = sb.merge_picks([dict(p) for p in raw])
    assert len(wrong_order) == 2, "pre-fix behaviour changed; update this test"
    assert all(e["consensus_count"] == 1 for e in wrong_order)

    right_order = _pipeline([dict(p) for p in raw])
    assert len(right_order) == 1 and right_order[0]["consensus_count"] == 2


def test_pipeline_repairs_matchups_before_merging():
    """The order is the fix. Guard it in the source itself."""
    body = inspect.getsource(sb._run_pipeline)
    repair_at = body.index("repair_matchups(picks")
    merge_at = body.index("merge_picks(picks)")
    assert repair_at < merge_at, (
        "repair_matchups must canonicalise matchups BEFORE merge_picks keys on "
        "them, or consensus becomes undetectable again"
    )


# --------------------------------------------------------------------------
# The other half: never manufacture a consensus that nobody posted
# --------------------------------------------------------------------------

def test_two_different_totals_on_one_game_stay_two_bets():
    """Real pair from the board: OVER 8 and OVER 5.5 on Mets @ Marlins."""
    merged = _pipeline([
        _pick("NY Mets @ MIA Marlins", "OVER 8", "cappersforfree"),
        _pick("New York Mets @ Miami Marlins", "OVER 5.5", "PropCaddie"),
    ])
    assert len(merged) == 2
    assert all(e["consensus_count"] == 1 for e in merged)


def test_over_and_under_on_the_same_number_stay_two_bets():
    merged = _pipeline([
        _pick("NY Mets @ MIA Marlins", "OVER 8", "a"),
        _pick("New York Mets @ Miami Marlins", "UNDER 8", "b"),
    ])
    assert len(merged) == 2


def test_opposing_spreads_of_equal_size_stay_two_bets():
    merged = _pipeline([
        _pick("LAA Angels @ TEX Rangers", "LAA Angels +1.5", "a"),
        _pick("Los Angeles Angels @ Texas Rangers", "Texas Rangers -1.5", "b"),
    ])
    assert len(merged) == 2


def test_different_player_props_never_merge():
    """Two props on one game are two bets, however similarly they're worded."""
    merged = _pipeline([
        _pick("MIA Marlins @ ATL Braves", "Heriberto Hernandez HR", "a"),
        _pick("Miami Marlins @ Atlanta Braves", "Ronald Acuna HR", "b"),
    ])
    assert len(merged) == 2
    assert all(e["consensus_count"] == 1 for e in merged)


def test_identical_player_prop_from_two_handles_does_merge():
    merged = _pipeline([
        _pick("MIA Marlins @ ATL Braves", "Heriberto Hernandez HR", "a"),
        _pick("Miami Marlins @ Atlanta Braves", "Heriberto Hernandez HR", "b"),
    ])
    assert len(merged) == 1 and merged[0]["consensus_count"] == 2


def test_period_bet_does_not_merge_with_the_full_game():
    merged = _pipeline([
        _pick("NY Mets @ MIA Marlins", "OVER 5.5", "a"),
        _pick("New York Mets @ Miami Marlins", "1st half OVER 5.5", "b"),
    ])
    assert len(merged) == 2


def test_moneyline_written_with_its_price_is_still_a_moneyline():
    """"TEX -150" is a price, not a 150-run handicap."""
    merged = _pipeline([
        _pick("LAA Angels @ TEX Rangers", "TEX Rangers -150", "a"),
        _pick("Los Angeles Angels @ Texas Rangers", "Texas Rangers ML", "b"),
    ])
    assert len(merged) == 1, [e["side"] for e in merged]
    assert merged[0]["consensus_count"] == 2


def test_spread_and_moneyline_on_one_team_stay_two_bets():
    merged = _pipeline([
        _pick("LAA Angels @ TEX Rangers", "TEX Rangers -1.5", "a"),
        _pick("Los Angeles Angels @ Texas Rangers", "Texas Rangers ML", "b"),
    ])
    assert len(merged) == 2


# --------------------------------------------------------------------------
# Degraded slate
# --------------------------------------------------------------------------

def test_without_a_slate_consensus_is_undetectable_and_that_is_documented():
    """No Odds API -> no canonical names -> spellings differ -> 1 sharp each.

    This is a real consequence of an expired key, so the auth-failure alert
    has to say so.
    """
    merged = _pipeline([
        _pick("MIL Brewers @ CHC Cubs", "MIL Brewers ML", "a"),
        _pick("Milwaukee Brewers @ Chicago Cubs", "Milwaukee ML", "b"),
    ], games=[])
    assert len(merged) == 2

    src = inspect.getsource(sb._run_pipeline)
    assert "consensus detection" in src, (
        "the Odds API auth-failure alert must name consensus detection as one "
        "of the things that stops"
    )


# --------------------------------------------------------------------------
# _side_line
# --------------------------------------------------------------------------

@pytest.mark.parametrize("side,expected", [
    ("OVER 8", 8.0),
    ("OVER 8.5", 8.5),
    ("o8.5", 8.5),
    ("Under 7", 7.0),
    ("LAA Angels +1.5", 1.5),
    ("Texas Rangers -1.5", 1.5),
    ("TEX Rangers -150", None),
    ("Texas Rangers ML", None),
    ("Teoscar Hernandez HR", None),
])
def test_side_line(side, expected):
    assert sb._side_line(side) == expected


# --------------------------------------------------------------------------
# Why the board actually went quiet
# --------------------------------------------------------------------------

def test_empty_enrichment_is_named_as_the_reason_nothing_qualified():
    """SP-20260908-1501: 18 picks, 5 independent handles, 18 genuinely
    different bets — and 18 of 18 with no enrichment at all.

    The quality bar is "2+ sharps OR one sharp at medium/high confidence", and
    that confidence comes only from enrichment. With enrichment empty the
    second clause can never fire, so unless two handles happen to land on the
    same bet, nothing can qualify no matter how good the board is. Ivy has to
    say that, rather than let a broken step read as a quiet slate.
    """
    src = inspect.getsource(sb._run_pipeline)
    assert 'if not any((e.get("enrichment") or {}).get("confidence") for e in merged)' in src
    assert "broken step, not a quiet" in src
