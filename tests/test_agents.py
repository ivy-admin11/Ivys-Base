"""Proactive agents: standardized run() signature, fake-pick removal, real
PDF attachment (not just text-with-a-false-claim). Every test mocks
messaging/LLM/PDF calls — none of these send a real iMessage or call a
real external API.
"""

import inspect

import pytest

from proactive_agents import Familia_meal_planner, happy_hour_scout, sports_bettor

AGENT_MODULES = [sports_bettor, happy_hour_scout, Familia_meal_planner]


@pytest.fixture
def fake_pdf(tmp_path):
    """Create a temporary fake PDF file for testing."""
    pdf_path = tmp_path / "fake.pdf"
    pdf_path.write_bytes(b"fake PDF content")
    return str(pdf_path)


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

    assert result["result_type"] == "no_picks"
    assert result["sent"] is False
    assert sent == []


def test_sports_bettor_sends_picks_when_available(monkeypatch):
    monkeypatch.setattr(sports_bettor, "fetch_live_odds", lambda: ["game1"])
    monkeypatch.setattr(sports_bettor, "sweep_with_retry", lambda games: [{"account": "@real", "matchup": "A vs B"}])
    # Create a consensus pick (2+ sharps) to meet quality threshold
    monkeypatch.setattr(sports_bettor, "merge_picks", lambda picks: [
        {"is_consensus": True, "consensus_count": 2}
    ])
    monkeypatch.setattr(sports_bettor, "attach_odds", lambda merged, games: None)
    monkeypatch.setattr(sports_bettor, "enrich_picks", lambda merged, games: None)
    monkeypatch.setattr(sports_bettor, "_report_signature", lambda merged: "sig-1")
    monkeypatch.setattr(sports_bettor, "load_last_report", lambda: {})
    monkeypatch.setattr(sports_bettor, "save_last_report", lambda sig, msg: None)
    monkeypatch.setattr(sports_bettor, "save_picks", lambda picks, **k: None)

    send_calls = []
    monkeypatch.setattr(
        sports_bettor, "send_imessage",
        lambda phone, text, **k: send_calls.append((phone, text)) or True,
    )

    result = sports_bettor.run(force=True, send=True)

    assert send_calls, "send_imessage was never called — picks were not sent"
    assert result["sent"] is True
    assert result["status"] == "success"


def test_sports_bettor_rejects_low_quality_picks(monkeypatch):
    """Verify that picks below quality threshold are rejected and no message is sent."""
    monkeypatch.setattr(sports_bettor, "fetch_live_odds", lambda: ["game1"])
    monkeypatch.setattr(sports_bettor, "sweep_with_retry", lambda games: [{"account": "@real", "matchup": "A vs B"}])
    # Create a non-consensus pick that doesn't meet quality threshold
    monkeypatch.setattr(sports_bettor, "merge_picks", lambda picks: [
        {"is_consensus": False, "consensus_count": 1}  # Single pick, below threshold
    ])
    monkeypatch.setattr(sports_bettor, "attach_odds", lambda merged, games: None)
    monkeypatch.setattr(sports_bettor, "enrich_picks", lambda merged, games: None)
    monkeypatch.setattr(sports_bettor, "load_last_report", lambda: {})
    monkeypatch.setattr(sports_bettor, "save_last_report", lambda sig, msg: None)
    monkeypatch.setattr(sports_bettor, "save_picks", lambda picks, **k: None)

    send_calls = []
    monkeypatch.setattr(
        sports_bettor, "send_imessage",
        lambda phone, text, **k: send_calls.append((phone, text)) or True,
    )

    result = sports_bettor.run(force=True, send=True)

    # Low-quality picks should result in no picks qualifying, so no message sent
    assert not send_calls, "send_imessage should not be called for low-quality picks"
    # Check backward-compatible result_type field for legacy API contracts
    assert result["result_type"] == "no_picks", "result_type should be 'no_picks' for backward compatibility"
    # Also verify status uses new enum format
    assert result["status"] == "no_qualifying_picks", "status should use new enum format"





def test_familia_meal_planner_attaches_pdf_not_just_text(monkeypatch, fake_pdf):
    monkeypatch.setattr(Familia_meal_planner, "check_48h_gate", lambda force=False: True)
    monkeypatch.setattr(
        Familia_meal_planner, "generate_family_meal_plan",
        lambda: {"status": "success", "recipe_count": 2, "recipes": []},
    )
    monkeypatch.setattr(Familia_meal_planner, "format_meal_plan_pdf", lambda data: fake_pdf)
    monkeypatch.setattr(Familia_meal_planner, "load_state", lambda: {"execution_history": []})
    monkeypatch.setattr(Familia_meal_planner, "save_state", lambda state: None)

    attach_calls = []
    monkeypatch.setattr(
        Familia_meal_planner, "send_imessage_attachment",
        lambda phone, path, **k: attach_calls.append((phone, path)) or True,
    )
    monkeypatch.setattr(Familia_meal_planner, "send_imessage", lambda *a, **k: True)

    result = Familia_meal_planner.run(force=True, send=True)

    assert attach_calls, "send_imessage_attachment was never called — PDF was never actually attached"
    assert result["status"] == "success"


def test_familia_meal_planner_force_bypasses_48h_gate():
    assert Familia_meal_planner.check_48h_gate(force=True) is True


def test_happy_hour_scout_attaches_pdf_not_just_text(monkeypatch, fake_pdf):
    monkeypatch.setattr(
        happy_hour_scout, "fetch_local_specials",
        lambda: {"venues": [{"name": "Bar"}], "specials": [{"detail": "half off"}]},
    )
    monkeypatch.setattr(happy_hour_scout, "format_happy_hour_pdf", lambda data: fake_pdf)

    attach_calls = []
    monkeypatch.setattr(
        happy_hour_scout, "send_imessage_attachment",
        lambda phone, path, **k: attach_calls.append((phone, path)) or True,
    )
    monkeypatch.setattr(happy_hour_scout, "send_imessage", lambda *a, **k: True)

    result = happy_hour_scout.run(force=True, send=True)

    assert attach_calls, "send_imessage_attachment was never called — PDF was never actually attached"
    assert result["status"] == "success"

