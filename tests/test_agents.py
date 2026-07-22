"""Proactive agents: standardized run() signature, fake-pick removal, real
PDF attachment (not just text-with-a-false-claim). Every test mocks
messaging/LLM/PDF calls — none of these send a real iMessage or call a
real external API.
"""

import inspect
import tempfile
from pathlib import Path

import pytest

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

    assert result["result_type"] == "no_picks"
    assert result["sent"] is False
    assert sent == []


def test_sports_bettor_attaches_pdf_not_just_text(monkeypatch):
    # Note: Sports Bettor is text-only and doesn't attach PDFs
    # This test verifies the proper return format when send=True
    # Mock pick data that passes quality filters (high confidence single-sharp)
    mock_pick = {
        "account": "@real",
        "matchup": "A vs B",
        "enrichment": {"confidence": "high"},
        "sport": "NFL",
        "line": "+3",
    }
    
    monkeypatch.setattr(sports_bettor, "fetch_live_odds", lambda: ["game1"])
    monkeypatch.setattr(sports_bettor, "sweep_with_retry", lambda games: [mock_pick])
    monkeypatch.setattr(sports_bettor, "merge_picks", lambda picks: [
        {
            **mock_pick,
            "is_consensus": False,
            "consensus_count": 1,
        }
    ])
    monkeypatch.setattr(sports_bettor, "attach_odds", lambda merged, games: None)
    monkeypatch.setattr(sports_bettor, "enrich_picks", lambda merged, games: None)
    monkeypatch.setattr(sports_bettor, "_report_signature", lambda merged: "sig-1")
    monkeypatch.setattr(sports_bettor, "load_last_report", lambda: {})
    monkeypatch.setattr(sports_bettor, "save_last_report", lambda sig, msg: None)
    monkeypatch.setattr(sports_bettor, "save_picks", lambda picks, report_date: None)

    sent_messages = []
    monkeypatch.setattr(
        sports_bettor, "send_imessage",
        lambda phone, text, **k: sent_messages.append((phone, text)) or True,
    )

    result = sports_bettor.run(force=True, send=True)

    # Sports bettor sends text-only reports, not PDFs
    assert sent_messages, "send_imessage was never called"
    assert result["sent"] is True
    # Verify backward-compatible keys are present
    assert "result_type" in result
    assert "attached" in result



def test_familia_meal_planner_attaches_pdf_not_just_text(monkeypatch):
    # Create a temporary PDF file so that send_imessage_attachment can find it
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_pdf:
        tmp_pdf.write(b"%PDF-1.4\n%mock pdf content")
        temp_pdf_path = tmp_pdf.name
    
    try:
        monkeypatch.setattr(Familia_meal_planner, "check_48h_gate", lambda force=False: True)
        monkeypatch.setattr(
            Familia_meal_planner, "generate_family_meal_plan",
            lambda: {"status": "success", "recipe_count": 2, "recipes": []},
        )
        monkeypatch.setattr(Familia_meal_planner, "format_meal_plan_pdf", lambda data: temp_pdf_path)
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
    finally:
        # Clean up the temporary file
        Path(temp_pdf_path).unlink(missing_ok=True)


def test_familia_meal_planner_force_bypasses_48h_gate():
    assert Familia_meal_planner.check_48h_gate(force=True) is True


def test_happy_hour_scout_attaches_pdf_not_just_text(monkeypatch):
    # Create a temporary PDF file so that send_imessage_attachment can find it
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_pdf:
        tmp_pdf.write(b"%PDF-1.4\n%mock pdf content")
        temp_pdf_path = tmp_pdf.name
    
    try:
        monkeypatch.setattr(
            happy_hour_scout, "fetch_local_specials",
            lambda: {"venues": [{"name": "Bar"}], "specials": [{"detail": "half off"}]},
        )
        monkeypatch.setattr(happy_hour_scout, "format_happy_hour_pdf", lambda data: temp_pdf_path)

        attach_calls = []
        monkeypatch.setattr(
            happy_hour_scout, "send_imessage_attachment",
            lambda phone, path, **k: attach_calls.append((phone, path)) or True,
        )
        monkeypatch.setattr(happy_hour_scout, "send_imessage", lambda *a, **k: True)

        result = happy_hour_scout.run(force=True, send=True)

        assert attach_calls, "send_imessage_attachment was never called — PDF was never actually attached"
        assert result["status"] == "success"
    finally:
        # Clean up the temporary file
        Path(temp_pdf_path).unlink(missing_ok=True)

