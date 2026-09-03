"""Proactive agents: standardized run() signature, fake-pick removal, and
text-first delivery — every job must put its content in the message body on
every run, and must never push a PDF attachment unasked. Every test mocks
messaging/LLM/PDF calls — none of these send a real iMessage or call a
real external API.
"""

import inspect

import pytest

from ivy_core import text_delivery
from proactive_agents import Familia_meal_planner, happy_hour_scout, sports_bettor

AGENT_MODULES = [sports_bettor, happy_hour_scout, Familia_meal_planner]


@pytest.mark.parametrize("module", AGENT_MODULES, ids=[m.__name__ for m in AGENT_MODULES])
def test_run_has_standardized_keyword_only_signature(module):
    sig = inspect.signature(module.run)
    for name in ("force", "send", "requester", "request_id"):
        assert name in sig.parameters, f"{module.__name__}.run missing param '{name}'"
        assert sig.parameters[name].kind == inspect.Parameter.KEYWORD_ONLY


def test_sports_bettor_has_no_fake_pick_injection():
    source = inspect.getsource(sports_bettor)
    assert "@Sharp1" not in source
    assert "HR Derby" not in source
    assert "TEST INJECTION" not in source


def test_sports_bettor_no_picks_does_not_send_when_send_false(monkeypatch):
    monkeypatch.setattr(sports_bettor, "fetch_live_odds", lambda: [])
    monkeypatch.setattr(sports_bettor, "sweep_with_retry", lambda games: [])
    sent = []
    monkeypatch.setattr(sports_bettor, "send_imessage", lambda *a, **k: sent.append(a) or True)

    result = sports_bettor.run(force=True, send=False)

    from ivy_core.pipeline_status import PipelineStatus

    assert result["status"] == PipelineStatus.NO_QUALIFYING_PICKS.value
    assert result["picks"] == 0
    assert result["sent"] is False
    assert sent == []


def test_sports_bettor_texts_the_picks_and_never_pushes_a_pdf(monkeypatch, tmp_path):
    """The regression that started all this: a PDF-only send that came back
    'submitted_unverified' was treated as success, so nothing was ever texted
    and Henry got silence. Picks must now always arrive as message text."""
    fake_pdf = tmp_path / "fake_picks.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(sports_bettor._outbox, "OUTBOX_DIR", tmp_path / "outbox")
    monkeypatch.setattr(sports_bettor, "fetch_live_odds", lambda: ["game1"])
    monkeypatch.setattr(sports_bettor, "sweep_with_retry", lambda games: [{"account": "@real", "matchup": "A vs B"}])
    monkeypatch.setattr(
        sports_bettor, "merge_picks",
        lambda picks: [{
            "is_consensus": False, "consensus_count": 1,
            "enrichment": {"confidence": "high", "take": "Sharp side."},
            "sport": "MLB", "matchup": "A vs B", "side": "A", "odds": "-110",
            "handicappers": ["realSharp"],
        }],
    )
    monkeypatch.setattr(sports_bettor, "save_picks", lambda picks, report_date=None: None)
    monkeypatch.setattr(sports_bettor, "attach_odds", lambda merged, games: None)
    monkeypatch.setattr(sports_bettor, "enrich_picks", lambda merged, games: None)
    monkeypatch.setattr(sports_bettor, "_report_signature", lambda merged: "sig-1")
    monkeypatch.setattr(sports_bettor, "load_last_report", lambda: {})
    saved = []
    monkeypatch.setattr(sports_bettor, "save_last_report", lambda sig, msg: saved.append((sig, msg)))
    monkeypatch.setattr(sports_bettor, "format_picks_pdf", lambda merged: str(fake_pdf))

    sent = []
    monkeypatch.setattr(
        text_delivery, "send_imessage",
        lambda phone, body: sent.append((phone, body)) or True,
    )
    monkeypatch.setattr(sports_bettor, "send_imessage", lambda *a, **k: True)

    result = sports_bettor.run(force=True, send=True)

    assert sent, "the picks were never texted"
    joined = "\n".join(b for _, b in sent)
    assert "A vs B" in joined and "-110" in joined, "the text didn't carry the actual pick"
    assert result["sent"] is True
    assert result["attached"] is False, "a PDF must not be pushed unasked"
    # The fingerprint is only stamped once the text actually went out.
    assert saved and saved[0][1] != saved[0][0], "last-report body must be the message, not the hash"


def test_sports_bettor_does_not_stamp_fingerprint_when_text_fails(monkeypatch, tmp_path):
    """A failed send must leave the slate resendable on the next run."""
    monkeypatch.setattr(sports_bettor._outbox, "OUTBOX_DIR", tmp_path / "outbox")
    monkeypatch.setattr(sports_bettor, "fetch_live_odds", lambda: ["game1"])
    monkeypatch.setattr(sports_bettor, "sweep_with_retry", lambda games: [{"account": "@real"}])
    monkeypatch.setattr(
        sports_bettor, "merge_picks",
        lambda picks: [{
            "is_consensus": True, "consensus_count": 2,
            "enrichment": {"confidence": "high"},
            "sport": "NFL", "matchup": "C vs D", "side": "C", "odds": "+100",
        }],
    )
    monkeypatch.setattr(sports_bettor, "save_picks", lambda picks, report_date=None: None)
    monkeypatch.setattr(sports_bettor, "attach_odds", lambda merged, games: None)
    monkeypatch.setattr(sports_bettor, "enrich_picks", lambda merged, games: None)
    monkeypatch.setattr(sports_bettor, "load_last_report", lambda: {})
    monkeypatch.setattr(sports_bettor, "format_picks_pdf", lambda merged: None)
    saved = []
    monkeypatch.setattr(sports_bettor, "save_last_report", lambda sig, msg: saved.append(sig))
    monkeypatch.setattr(text_delivery, "send_imessage", lambda phone, body: False)
    monkeypatch.setattr(sports_bettor, "send_imessage", lambda *a, **k: True)

    result = sports_bettor.run(force=True, send=True)

    assert saved == [], "a failed delivery must not suppress the next run"
    assert result["sent"] is False


def test_picks_digest_is_concise_and_keeps_every_pick_in_detail():
    picks = [
        {"sport": "MLB", "matchup": f"T{i} vs U{i}", "side": "ML", "odds": "-110",
         "is_consensus": i < 2, "consensus_count": 3 if i < 2 else 1,
         "handicappers": ["a", "b", "c"][: 3 if i < 2 else 1],
         "enrichment": {"confidence": "high"}}
        for i in range(9)
    ]
    body, detail = sports_bettor.format_picks_digest(picks)

    assert len(body) <= 1200, "the first bubble must stay skimmable"
    assert body.count("\n1. ") + body.startswith("1. ") >= 1
    assert "reply MORE" in body, "held-back picks must be discoverable"
    assert len(detail["items"]) == 9, "MORE/WHY must be able to reach every pick"
    assert detail["shown"] == sports_bettor.DIGEST_TOP_N
    assert all(item["detail"] for item in detail["items"]), "every pick needs a WHY answer"


def test_familia_meal_planner_texts_the_plan_and_never_pushes_a_pdf(monkeypatch, tmp_path):
    fake_pdf = tmp_path / "fake_meal.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(Familia_meal_planner._outbox, "OUTBOX_DIR", tmp_path / "outbox")
    monkeypatch.setattr(Familia_meal_planner, "check_48h_gate", lambda force=False: True)
    monkeypatch.setattr(
        Familia_meal_planner, "generate_family_meal_plan",
        lambda: {"status": "success", "recipe_count": 2, "recipes": [
            {"recipe_name": "Arepas de Pollo", "cuisine_origin": "Venezuelan",
             "prep_time_minutes": 15, "cooking_time_minutes": 20,
             "toddler_adaptations": ["shred the chicken"]},
            {"recipe_name": "Miso Butter Salmon", "cuisine_origin": "Asian fusion",
             "prep_time_minutes": 10, "cooking_time_minutes": 15},
        ]},
    )
    monkeypatch.setattr(Familia_meal_planner, "format_meal_plan_pdf", lambda data: str(fake_pdf))
    monkeypatch.setattr(Familia_meal_planner, "load_state", lambda: {"execution_history": []})
    monkeypatch.setattr(Familia_meal_planner, "save_state", lambda state: None)

    sent = []
    monkeypatch.setattr(
        text_delivery, "send_imessage",
        lambda phone, body: sent.append((phone, body)) or True,
    )
    monkeypatch.setattr(Familia_meal_planner, "send_imessage", lambda *a, **k: True)

    result = Familia_meal_planner.run(force=True, send=True)

    joined = "\n".join(b for _, b in sent)
    assert "Arepas de Pollo" in joined, "the plan was never texted"
    assert result["status"] == "success"


def test_familia_meal_planner_force_bypasses_48h_gate():
    assert Familia_meal_planner.check_48h_gate(force=True) is True


def test_happy_hour_scout_texts_the_specials_and_never_pushes_a_pdf(monkeypatch, tmp_path):
    fake_pdf = tmp_path / "fake_hh.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(happy_hour_scout._outbox, "OUTBOX_DIR", tmp_path / "outbox")
    monkeypatch.setattr(
        happy_hour_scout, "fetch_local_specials",
        lambda: {
            "venues": [{"name": "Hudson House", "region": "Frisco, TX"}],
            "specials": [{"venue": "Hudson House", "detail": "half-price oysters",
                          "days_hours": "Mon-Fri 3-6"}],
        },
    )
    monkeypatch.setattr(happy_hour_scout, "format_happy_hour_pdf", lambda data: str(fake_pdf))

    sent = []
    monkeypatch.setattr(
        text_delivery, "send_imessage",
        lambda phone, body: sent.append((phone, body)) or True,
    )
    monkeypatch.setattr(happy_hour_scout, "send_imessage", lambda *a, **k: True)

    result = happy_hour_scout.run(force=True, send=True)

    joined = "\n".join(b for _, b in sent)
    assert "Hudson House" in joined and "oysters" in joined, "the specials were never texted"
    assert result["status"] == "success"


def test_sports_bettor_speaks_up_when_picks_exist_but_none_qualify(monkeypatch, tmp_path):
    """The most recent real run: 7 picks swept, 0 cleared the threshold, and the
    job returned in total silence — indistinguishable from not running."""
    monkeypatch.setattr(sports_bettor._outbox, "OUTBOX_DIR", tmp_path / "outbox")
    monkeypatch.setattr(sports_bettor, "fetch_live_odds", lambda: ["game1"])
    monkeypatch.setattr(sports_bettor, "sweep_with_retry", lambda games: [{"account": "@real"}])
    monkeypatch.setattr(
        sports_bettor, "merge_picks",
        lambda picks: [{
            "is_consensus": False, "consensus_count": 1,
            "enrichment": {"confidence": "low"},
            "sport": "MLB", "matchup": f"E{i} vs F{i}", "side": "ML", "odds": "-110",
        } for i in range(7)],
    )
    monkeypatch.setattr(sports_bettor, "save_picks", lambda picks, report_date=None: None)
    monkeypatch.setattr(sports_bettor, "attach_odds", lambda merged, games: None)
    monkeypatch.setattr(sports_bettor, "enrich_picks", lambda merged, games: None)
    monkeypatch.setattr(sports_bettor, "load_last_report", lambda: {})
    monkeypatch.setattr(sports_bettor, "save_last_report", lambda sig, msg: None)

    sent = []
    monkeypatch.setattr(
        text_delivery, "send_imessage",
        lambda phone, body: sent.append(body) or True,
    )
    monkeypatch.setattr(sports_bettor, "send_imessage", lambda *a, **k: True)

    result = sports_bettor.run(force=True, send=True)

    joined = "\n".join(sent)
    assert sent, "a swept-but-filtered board must still be reported"
    assert "E0 vs F0" in joined
    assert "cleared the bar" in joined, "the message must not read like a play"
    assert result["picks"] == 0


def test_sports_bettor_stays_quiet_on_an_unchanged_below_bar_board(monkeypatch, tmp_path):
    monkeypatch.setattr(sports_bettor._outbox, "OUTBOX_DIR", tmp_path / "outbox")
    monkeypatch.setattr(sports_bettor, "fetch_live_odds", lambda: ["game1"])
    monkeypatch.setattr(sports_bettor, "sweep_with_retry", lambda games: [{"account": "@real"}])
    monkeypatch.setattr(
        sports_bettor, "merge_picks",
        lambda picks: [{
            "is_consensus": False, "consensus_count": 1,
            "enrichment": {"confidence": "low"},
            "sport": "MLB", "matchup": "G vs H", "side": "ML", "odds": "-110",
        }],
    )
    monkeypatch.setattr(sports_bettor, "save_picks", lambda picks, report_date=None: None)
    monkeypatch.setattr(sports_bettor, "attach_odds", lambda merged, games: None)
    monkeypatch.setattr(sports_bettor, "enrich_picks", lambda merged, games: None)
    monkeypatch.setattr(sports_bettor, "save_last_report", lambda sig, msg: None)

    picks_for_sig = [{
        "is_consensus": False, "consensus_count": 1,
        "sport": "MLB", "matchup": "G vs H", "side": "ML", "odds": "-110",
    }]
    monkeypatch.setattr(
        sports_bettor, "load_last_report",
        lambda: {"signature": sports_bettor._report_signature(picks_for_sig)},
    )

    sent = []
    monkeypatch.setattr(text_delivery, "send_imessage", lambda phone, body: sent.append(body) or True)
    monkeypatch.setattr(sports_bettor, "send_imessage", lambda *a, **k: True)

    sports_bettor.run(force=False, send=True)

    assert sent == [], "an unchanged below-the-bar board must not nag"


def test_player_props_do_not_borrow_the_game_market_price():
    """Live run 2026-09-02: 'David Peterson Under 4.5 Strikeouts' was texted with
    '(Over 8 (-117) / Under 8 (-103))' — the game's run total, not the prop's
    price, and a contradicting number besides."""
    games = [{
        "away": "Milwaukee Brewers", "home": "Chicago Cubs", "sport": "MLB",
        "total": "Over 8 (-117) / Under 8 (-103)",
        "moneyline": "MIL +105 / CHC -125", "spread": "MIL +1.5",
        "commence": "2026-09-03T23:15:00Z",
    }]
    picks = [
        {"matchup": "Milwaukee Brewers @ Chicago Cubs",
         "side": "David Peterson Under 4.5 Strikeouts"},
        {"matchup": "Milwaukee Brewers @ Chicago Cubs", "side": "Under 8.5"},
    ]
    sports_bettor.attach_odds(picks, games)

    assert not picks[0].get("odds"), "a prop must not inherit the game total"
    assert picks[0]["sport"] == "MLB", "sport and start time still backfill"
    assert picks[0]["start"] == "2026-09-03T23:15:00Z"
    assert picks[1]["odds"] == "Over 8 (-117) / Under 8 (-103)", "real game totals still fill"
