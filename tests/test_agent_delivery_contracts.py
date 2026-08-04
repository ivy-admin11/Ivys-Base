"""Delivery-result contracts for proactive agents.

Every sender and provider is mocked.  These tests never invoke Messages.app,
AppleScript, a live API, or a production outbox.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from ivy_core.job_worker import normalize_result
from ivy_core.report_fallback import AttachmentDeliveryReceipt
from proactive_agents import Familia_meal_planner, happy_hour_scout, sports_bettor


@pytest.fixture
def fake_pdf(tmp_path):
    path = tmp_path / "report.pdf"
    path.write_bytes(b"%PDF-1.4\n%%EOF")
    return str(path)


def _attachment_receipt(report_id: str, status: str) -> AttachmentDeliveryReceipt:
    kwargs = {
        "report_id": report_id,
        "attachment_path": "/tmp/report.pdf",
        "staged_path": "/tmp/report.pdf",
        "file_size_bytes": 100,
        "attempts": 1,
        "applescript_result": "TEST_ONLY",
    }
    if status == "failed":
        return AttachmentDeliveryReceipt.make_failed(
            **kwargs,
            error_code="TEST_FAILURE",
            error_detail="test-only failure",
        )
    if status == "verified_delivered":
        return AttachmentDeliveryReceipt.make_verified(**kwargs)
    return AttachmentDeliveryReceipt.make_unverified(**kwargs)


def _prepare_sharp_no_picks(monkeypatch) -> None:
    monkeypatch.setattr(sports_bettor, "fetch_live_odds", lambda: [])
    monkeypatch.setattr(sports_bettor, "sweep_with_retry", lambda _games: [])
    monkeypatch.setattr(
        "ivy_core.result_updater.auto_update_results",
        lambda: {"status": "skipped"},
    )


@pytest.mark.parametrize(
    ("submitted", "expected_status"),
    [(True, "submitted_unverified"), (False, "failed")],
)
def test_forced_sharp_no_picks_returns_actual_notice_result(
    monkeypatch,
    submitted,
    expected_status,
):
    _prepare_sharp_no_picks(monkeypatch)
    attempts = []
    monkeypatch.setattr(
        sports_bettor,
        "send_imessage",
        lambda phone, text: attempts.append((phone, text)) or submitted,
    )

    result = sports_bettor.run(force=True, send=True)

    assert len(attempts) == 1
    assert result["result_type"] == "no_picks"
    assert result["sent"] is submitted
    assert result["delivery_status"] == expected_status
    assert result["report_ids"] == []
    assert result["deliveries"] == [{
        "channel": "imessage_text",
        "purpose": "no_picks_notice",
        "status": expected_status,
    }]
    assert normalize_result(result, send=True)[2] == expected_status


def test_scheduled_sharp_no_picks_is_explicitly_not_attempted(monkeypatch):
    _prepare_sharp_no_picks(monkeypatch)
    monkeypatch.setattr(
        sports_bettor,
        "send_imessage",
        lambda *_args, **_kwargs: pytest.fail("unexpected live-send path"),
    )

    result = sports_bettor.run(force=False, send=True)

    assert result["sent"] is False
    assert result["delivery_status"] == "not_attempted"
    assert result["deliveries"] == []
    assert normalize_result(result, send=True)[2] == "not_attempted"


def test_forced_sharp_no_picks_sender_exception_is_unknown(monkeypatch):
    _prepare_sharp_no_picks(monkeypatch)

    def raise_after_attempt(*_args, **_kwargs):
        raise RuntimeError("test-only sender failure")

    monkeypatch.setattr(sports_bettor, "send_imessage", raise_after_attempt)

    result = sports_bettor.run(force=True, send=True)

    assert result["sent"] is False
    assert result["delivery_status"] == "unknown"
    assert result["deliveries"][0]["status"] == "unknown"
    assert result["deliveries"][0]["error_category"] == "RuntimeError"
    assert "test-only sender failure" not in str(result)


def _prepare_happy_hour(monkeypatch, fake_pdf: str) -> None:
    monkeypatch.setattr(
        happy_hour_scout,
        "fetch_local_specials",
        lambda: {"venues": [{"name": "test"}], "specials": [{"detail": "test"}]},
    )
    monkeypatch.setattr(happy_hour_scout, "format_happy_hour_pdf", lambda _data: fake_pdf)
    monkeypatch.setattr(happy_hour_scout._outbox, "save_report", lambda *_a, **_k: None)
    monkeypatch.setattr(
        happy_hour_scout._outbox,
        "update_report_status",
        lambda *_a, **_k: None,
    )


def _prepare_meal_plan(monkeypatch, fake_pdf: str) -> None:
    monkeypatch.setattr(Familia_meal_planner, "check_48h_gate", lambda force=False: True)
    monkeypatch.setattr(
        Familia_meal_planner,
        "generate_family_meal_plan",
        lambda: {"status": "success", "recipe_count": 2, "recipes": []},
    )
    monkeypatch.setattr(Familia_meal_planner, "format_meal_plan_pdf", lambda _data: fake_pdf)
    monkeypatch.setattr(
        Familia_meal_planner,
        "load_state",
        lambda: {"execution_history": []},
    )
    monkeypatch.setattr(Familia_meal_planner, "save_state", lambda _state: None)
    monkeypatch.setattr(Familia_meal_planner._outbox, "save_report", lambda *_a, **_k: None)
    monkeypatch.setattr(
        Familia_meal_planner._outbox,
        "update_report_status",
        lambda *_a, **_k: None,
    )


def _next_report_id(values: Iterator[str]):
    return lambda _job_name: next(values)


def test_happy_hour_returns_every_recipient_report_and_delivery(monkeypatch, fake_pdf):
    _prepare_happy_hour(monkeypatch, fake_pdf)
    monkeypatch.setattr(
        happy_hour_scout,
        "ALERT_RECIPIENTS",
        {"first": "test-recipient-1", "second": "test-recipient-2"},
    )
    monkeypatch.setattr(
        happy_hour_scout._outbox,
        "make_report_id",
        _next_report_id(iter(["HH-TEST-1", "HH-TEST-2"])),
    )
    monkeypatch.setattr(
        happy_hour_scout,
        "send_imessage_attachment",
        lambda _phone, _path, *, report_id: _attachment_receipt(
            report_id,
            "submitted_unverified",
        ),
    )
    text_results = iter([True, False])
    monkeypatch.setattr(
        happy_hour_scout,
        "send_imessage",
        lambda _phone, _text: next(text_results),
    )

    result = happy_hour_scout.run(force=True, send=True)

    assert result["report_ids"] == ["HH-TEST-1", "HH-TEST-2"]
    assert [item["report_id"] for item in result["deliveries"]] == result["report_ids"]
    assert [item["recipient"] for item in result["deliveries"]] == ["first", "second"]
    assert [item["notification_status"] for item in result["deliveries"]] == [
        "submitted_unverified",
        "failed",
    ]
    assert result["delivery_status"] == "submitted_unverified"
    assert result["alert_sent"] is True
    assert normalize_result(result, send=True)[3] == result["report_ids"]


def test_familia_mixed_recipient_outcomes_return_partial(monkeypatch, fake_pdf):
    _prepare_meal_plan(monkeypatch, fake_pdf)
    monkeypatch.setattr(
        Familia_meal_planner,
        "ALERT_RECIPIENTS",
        {"first": "test-recipient-1", "second": "test-recipient-2"},
    )
    monkeypatch.setattr(
        Familia_meal_planner._outbox,
        "make_report_id",
        _next_report_id(iter(["MP-TEST-1", "MP-TEST-2"])),
    )
    receipt_statuses = iter(["verified_delivered", "failed"])
    monkeypatch.setattr(
        Familia_meal_planner,
        "send_imessage_attachment",
        lambda _phone, _path, *, report_id: _attachment_receipt(
            report_id,
            next(receipt_statuses),
        ),
    )
    # First recipient's notification succeeds.  The second recipient receives
    # a failure notice, then its report fallback fails on the first bubble.
    text_results = iter([True, True, False])
    monkeypatch.setattr(
        Familia_meal_planner,
        "send_imessage",
        lambda _phone, _text: next(text_results),
    )
    monkeypatch.setattr(
        Familia_meal_planner,
        "split_imessage_content",
        lambda _text: ["fallback-1", "fallback-2"],
    )

    result = Familia_meal_planner.run(force=True, send=True)

    assert result["report_ids"] == ["MP-TEST-1", "MP-TEST-2"]
    assert [item["status"] for item in result["deliveries"]] == [
        "verified_delivered",
        "failed",
    ]
    assert result["deliveries"][1]["notice_status"] == "submitted_unverified"
    assert result["deliveries"][1]["fallback_messages_attempted"] == 1
    assert result["deliveries"][1]["fallback_messages_submitted"] == 0
    assert result["delivery_status"] == "partial"
    assert result["alert_sent"] is True
    assert normalize_result(result, send=True)[2] == "partial"


@pytest.mark.parametrize("module_name", ["happy_hour", "familia"])
def test_no_content_returns_not_attempted(monkeypatch, fake_pdf, module_name):
    if module_name == "happy_hour":
        monkeypatch.setattr(
            happy_hour_scout,
            "fetch_local_specials",
            lambda: {"venues": [], "specials": []},
        )
        monkeypatch.setattr(
            happy_hour_scout,
            "format_happy_hour_pdf",
            lambda _data: fake_pdf,
        )
        monkeypatch.setattr(
            happy_hour_scout,
            "send_imessage",
            lambda *_a, **_k: pytest.fail("unexpected send"),
        )
        result = happy_hour_scout.run(send=True)
    else:
        _prepare_meal_plan(monkeypatch, fake_pdf)
        monkeypatch.setattr(
            Familia_meal_planner,
            "generate_family_meal_plan",
            lambda: {"status": "success", "recipe_count": 0, "recipes": []},
        )
        monkeypatch.setattr(
            Familia_meal_planner,
            "send_imessage",
            lambda *_a, **_k: pytest.fail("unexpected send"),
        )
        result = Familia_meal_planner.run(force=True, send=True)

    assert result["alert_sent"] is False
    assert result["delivery_status"] == "not_attempted"
    assert result["deliveries"] == []
    assert result["report_ids"] == []
    assert normalize_result(result, send=True)[2] == "not_attempted"


def test_familia_gate_skip_returns_not_attempted(monkeypatch):
    monkeypatch.setattr(Familia_meal_planner, "check_48h_gate", lambda force=False: False)

    result = Familia_meal_planner.run(force=False, send=True)

    assert result["status"] == "skipped"
    assert result["delivery_status"] == "not_attempted"
    assert result["deliveries"] == []
    assert normalize_result(result, send=True)[2] == "not_attempted"
