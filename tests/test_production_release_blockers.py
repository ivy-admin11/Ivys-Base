"""Regression tests for production blockers discovered during final audit.

Every test is hermetic: no Messages.app, providers, live state files, or
production databases are used.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import job_runner
import main
from ivy_core.pipeline_status import ProviderUnavailableError
from proactive_agents import Familia_meal_planner, happy_hour_scout, sports_bettor


def _qualified_sports_pick() -> list[dict]:
    return [{"matchup": "Away @ Home", "side": "Away -1.5"}]


def _prepare_qualified_sports(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sports_bettor, "fetch_live_odds", lambda: [{"sport": "MLB"}])
    monkeypatch.setattr(sports_bettor, "sweep_with_retry", lambda _games: _qualified_sports_pick())
    monkeypatch.setattr(
        sports_bettor,
        "merge_picks",
        lambda _picks: [{
            "sport": "MLB",
            "matchup": "Away @ Home",
            "side": "Away -1.5",
            "odds": "-110",
            "handicappers": ["sharp-a", "sharp-b"],
            "confidence": "high",
            "consensus_count": 2,
            "is_consensus": True,
        }],
    )
    monkeypatch.setattr(sports_bettor, "attach_odds", lambda *_args: None)
    monkeypatch.setattr(sports_bettor, "enrich_picks", lambda *_args: None)
    monkeypatch.setattr(sports_bettor, "save_picks", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sports_bettor, "load_last_report", lambda: {})


def test_grok_outage_is_not_reported_as_quiet_handicappers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sports_bettor, "fetch_live_odds", lambda: [])
    monkeypatch.setattr(
        sports_bettor,
        "sweep_with_retry",
        lambda _games: (_ for _ in ()).throw(
            ProviderUnavailableError("Grok X Search", "unavailable")
        ),
    )
    sent: list[tuple] = []
    monkeypatch.setattr(sports_bettor, "send_imessage", lambda *args: sent.append(args) or True)

    result = sports_bettor.run(force=True, send=True)

    assert result["status"] == "upstream_unavailable"
    assert result["result_type"] == "upstream_unavailable"
    assert sent == []


def test_degraded_odds_empty_sweep_does_not_send_false_no_picks_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sports_bettor,
        "fetch_live_odds",
        lambda: (_ for _ in ()).throw(
            ProviderUnavailableError("The Odds API", "unavailable")
        ),
    )
    monkeypatch.setattr(sports_bettor, "sweep_with_retry", lambda _games: [])
    sent: list[tuple] = []
    monkeypatch.setattr(sports_bettor, "send_imessage", lambda *args: sent.append(args) or True)

    result = sports_bettor.run(force=True, send=True)

    assert result["status"] == "degraded"
    assert sent == []


def test_sports_report_is_not_sent_when_duplicate_reservation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_qualified_sports(monkeypatch)
    monkeypatch.setattr(sports_bettor, "save_last_report", lambda *_args: False)
    sent: list[tuple] = []
    monkeypatch.setattr(sports_bettor, "send_imessage", lambda *args: sent.append(args) or True)

    result = sports_bettor.run(force=True, send=True)

    assert result["status"] == "internal_error"
    assert result["sent"] is False
    assert sent == []


def test_ambiguous_sports_delivery_keeps_duplicate_reservation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _prepare_qualified_sports(monkeypatch)
    state_path = tmp_path / "sports_last_report.json"
    monkeypatch.setattr(sports_bettor, "LAST_REPORT_PATH", str(state_path))
    monkeypatch.setattr(
        sports_bettor,
        "send_imessage",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("ambiguous transport")),
    )

    result = sports_bettor._run_pipeline(force=True, send=True)

    assert result["status"] == "internal_error"
    assert "duplicate suppression remains reserved" in result["message"]
    assert state_path.is_file()


def test_happy_hour_total_provider_failure_is_not_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        happy_hour_scout,
        "query_llm",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    called: list[object] = []
    monkeypatch.setattr(happy_hour_scout, "format_happy_hour_pdf", lambda value: called.append(value))

    result = happy_hour_scout.run(send=False)

    assert result["status"] == "upstream_unavailable"
    assert called == []


def test_meal_plan_state_failure_blocks_outbound_delivery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(Familia_meal_planner, "check_48h_gate", lambda force=False: True)
    monkeypatch.setattr(
        Familia_meal_planner,
        "generate_family_meal_plan",
        lambda: {"status": "success", "recipe_count": 1, "recipes": []},
    )
    monkeypatch.setattr(Familia_meal_planner, "format_meal_plan_pdf", lambda _data: str(tmp_path / "plan.pdf"))
    monkeypatch.setattr(Familia_meal_planner, "load_state", lambda: {"execution_history": []})
    monkeypatch.setattr(Familia_meal_planner, "save_state", lambda _state: False)
    sent: list[tuple] = []
    monkeypatch.setattr(
        Familia_meal_planner,
        "send_imessage_attachment",
        lambda *args, **kwargs: sent.append(args),
    )

    result = Familia_meal_planner.run(force=True, send=True)

    assert result["status"] == "error"
    assert sent == []


def test_attachment_staging_uses_unique_private_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from ivy_core import messaging

    first = tmp_path / "one" / "report.pdf"
    second = tmp_path / "two" / "report.pdf"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    stage = tmp_path / "stage"
    staged_paths: list[Path] = []

    class Runner:
        last_error_category = None

        def send_imessage_file_argv(self, _recipient: str, path: str) -> str:
            staged_paths.append(Path(path))
            return "SUCCESS"

        # Delivery now tries the headless scripting verb before the paste
        # path; both must land on the same uniquely-staged file.
        def send_imessage_file_scripting_argv(self, _recipient: str, path: str) -> str:
            staged_paths.append(Path(path))
            return "SUCCESS"

    monkeypatch.setattr(messaging, "_IMSG_ATTACH_STAGE", str(stage))
    monkeypatch.setattr(messaging, "_runner", Runner())

    assert messaging.send_imessage_attachment("+15555550100", str(first)).status == "submitted_unverified"
    assert messaging.send_imessage_attachment("+15555550101", str(second)).status == "submitted_unverified"

    assert len(staged_paths) == 2
    assert staged_paths[0] != staged_paths[1]
    assert staged_paths[0].read_bytes() == b"first"
    assert staged_paths[1].read_bytes() == b"second"
    assert stage.stat().st_mode & 0o077 == 0


def test_put_request_is_not_supersedable_read_only_work(monkeypatch: pytest.MonkeyPatch) -> None:
    assert main.classify_imessage_text("Please put milk on my list") == "conversation_action"


def test_state_write_failure_after_successful_reply_does_not_send_second_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = main.InboundMessage(991, "help", "+15555550100", 0.0)
    unit = main.ProcessingUnit((message,), "ops_help")
    sent: list[str] = []
    monkeypatch.setattr(main, "_operations_reply", lambda _unit: "done")
    monkeypatch.setattr(main, "_is_superseded", lambda _unit: False)
    monkeypatch.setattr(main, "run_local_applescript_send", lambda _target, body: sent.append(body) or "SUCCESS")
    monkeypatch.setattr(main._IMESSAGE_STATE, "mark_processing", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(main._IMESSAGE_STATE, "mark_sending", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main._IMESSAGE_STATE, "mark_terminal", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("state unavailable")))

    main._process_imessage_unit(unit)

    assert sent == ["done"]


def test_stopped_ack_timer_never_sends_after_final_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    message = main.InboundMessage(992, "hello", "+15555550100", 0.0)
    unit = main.ProcessingUnit((message,), "conversation_read_only")
    sent: list[str] = []
    monkeypatch.setattr(main, "IMESSAGE_SLOW_ACK_SECONDS", 0.01)
    monkeypatch.setattr(main, "run_local_applescript_send", lambda _target, body: sent.append(body) or "SUCCESS")

    timer, finished, gate = main._start_slow_ack_timer(unit)
    main._stop_slow_ack_timer(timer, finished, gate)
    timer.join(timeout=0.2)

    assert sent == []


def test_every_provider_tool_call_is_executed_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        main,
        "_execute_tool_call",
        lambda name, arguments: calls.append((name, arguments)) or f"{name}:ok",
    )

    reply = main._execute_native_tool_calls(
        [
            {"function": {"name": "first", "arguments": '{"value": 1}'}},
            {"function": {"name": "second", "arguments": '{"value": 2}'}},
        ],
        "test",
    )

    assert calls == [("first", {"value": 1}), ("second", {"value": 2})]
    assert reply == "first:ok\nsecond:ok"


def test_detached_jobs_default_to_current_release_interpreter() -> None:
    import sys

    assert job_runner.VENV_PYTHON == Path(sys.executable).resolve()
