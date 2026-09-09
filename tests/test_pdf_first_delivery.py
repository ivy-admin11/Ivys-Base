"""PDF-first delivery, and the guard that makes it safe.

Henry asked for Sharp Picks to go back to PDF-first on 2026-09-09: a short
covering text, the board in the attachment. That is the configuration that lost
three consecutive reports in Aug/Sep 2026 -- SP-20260828-2107, SP-20260829-1502
and SP-20260901-1525 were all recorded as sent while he received nothing --
because the old code branched on the receipt's truthiness and
``AttachmentDeliveryReceipt.__bool__`` is True for ``submitted_unverified``.

The difference now is one comparison: only ``verified_delivered`` counts.
Everything else sends the full board as text. These tests exist to keep that
comparison from ever being loosened back into a truthiness check.
"""

import pytest

from ivy_core import text_delivery


class _Receipt:
    def __init__(self, status):
        self.status = status

    def __bool__(self):
        # Faithful to the real receipt: truthy for BOTH verified and
        # unverified. A test that used `if receipt:` would pass on the broken
        # behaviour, so nothing here may rely on truthiness.
        return self.status in ("verified_delivered", "submitted_unverified")


@pytest.fixture
def outbox(tmp_path, monkeypatch):
    monkeypatch.setattr(text_delivery._outbox, "OUTBOX_DIR", tmp_path / "outbox")
    return tmp_path


def _send(sent):
    def _s(phone, body):
        sent.append(body)
        return True
    return _s


def _deliver(sent, receipt_status, tmp_path, **kw):
    pdf = tmp_path / "board.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    calls = []

    def _attach(phone, path, report_id=None):
        calls.append(path)
        if receipt_status is None:
            raise RuntimeError("AppleScript blew up")
        return _Receipt(receipt_status)

    result = text_delivery.deliver_report(
        "+15555550100",
        job_name="sharp_picks",
        body="SHORT COVERING MESSAGE",
        pdf_path=str(pdf),
        attach_pdf=True,
        fallback_body="FULL BOARD: A @ B — A ML",
        sender=_send(sent),
        attachment_sender=_attach,
        **kw,
    )
    return result, calls


def test_verified_attachment_sends_no_fallback_text(outbox):
    sent = []
    result, calls = _deliver(sent, "verified_delivered", outbox)
    assert calls, "the PDF was never attempted"
    assert result.attachment_status == text_delivery.ATTACH_VERIFIED
    assert result.fallback_sent is False
    assert not any("FULL BOARD" in b for b in sent), "board texted despite a delivered PDF"
    assert result.content_reached_henry is True


def test_unverified_attachment_sends_the_board_as_text(outbox):
    """The exact failure mode. submitted_unverified is NOT success."""
    sent = []
    result, _ = _deliver(sent, "submitted_unverified", outbox)
    assert result.attachment_status == text_delivery.ATTACH_UNCONFIRMED
    assert result.fallback_sent is True
    assert any("FULL BOARD" in b for b in sent), "the board never reached Henry"
    assert any("didn't confirm" in b for b in sent)
    assert result.content_reached_henry is True


def test_failed_attachment_sends_the_board_as_text(outbox):
    sent = []
    result, _ = _deliver(sent, "failed", outbox)
    assert result.attachment_status == text_delivery.ATTACH_UNCONFIRMED
    assert result.fallback_sent is True
    assert any("FULL BOARD" in b for b in sent)


def test_attachment_raising_still_sends_the_board(outbox):
    sent = []
    result, _ = _deliver(sent, None, outbox)
    assert result.attachment_status == text_delivery.ATTACH_FAILED
    assert result.fallback_sent is True
    assert any("FULL BOARD" in b for b in sent)


def test_receipt_truthiness_is_not_what_decides(outbox):
    """submitted_unverified is truthy; it must still take the fallback path."""
    assert bool(_Receipt("submitted_unverified")) is True
    sent = []
    result, _ = _deliver(sent, "submitted_unverified", outbox)
    assert result.fallback_sent is True, (
        "the truthiness of the receipt decided the branch again -- this is the "
        "regression that lost three reports"
    )


def test_content_reached_henry_is_false_when_both_routes_fail(outbox, tmp_path):
    """Neither PDF nor text got there: the caller must not stamp the fingerprint."""
    pdf = tmp_path / "board.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    sent = []

    def _sender(phone, body):
        # The covering message goes; the fallback board does not.
        if "FULL BOARD" in body:
            return False
        sent.append(body)
        return True

    result = text_delivery.deliver_report(
        "+15555550100",
        job_name="sharp_picks",
        body="SHORT",
        pdf_path=str(pdf),
        attach_pdf=True,
        fallback_body="FULL BOARD",
        sender=_sender,
        attachment_sender=lambda *a, **k: _Receipt("submitted_unverified"),
    )
    assert result.delivered is True, "the covering text did send"
    assert result.fallback_sent is False
    assert result.content_reached_henry is False, (
        "delivered is True but the board reached Henry by neither route -- "
        "stamping the fingerprint here would suppress the next run's retry"
    )


def test_text_first_remains_the_default(outbox, tmp_path):
    """Every other job is untouched: no attach_pdf, no attachment attempted."""
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    calls = []
    sent = []
    result = text_delivery.deliver_report(
        "+15555550100",
        job_name="familia_meal_planner",
        body="THE WHOLE PLAN",
        pdf_path=str(pdf),
        sender=_send(sent),
        attachment_sender=lambda *a, **k: calls.append(1) or _Receipt("verified_delivered"),
    )
    assert calls == [], "a PDF was pushed by a job that never asked for it"
    assert result.attachment_status == text_delivery.ATTACH_SKIPPED
    assert result.content_reached_henry is True
    assert any("THE WHOLE PLAN" in b for b in sent)


def test_no_attachment_attempted_when_the_covering_text_failed(outbox, tmp_path):
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    calls = []
    result = text_delivery.deliver_report(
        "+15555550100",
        job_name="sharp_picks",
        body="SHORT",
        pdf_path=str(pdf),
        attach_pdf=True,
        fallback_body="FULL BOARD",
        sender=lambda phone, body: False,
        attachment_sender=lambda *a, **k: calls.append(1) or _Receipt("verified_delivered"),
    )
    assert calls == [], "attached a PDF to a conversation the text never reached"
    assert result.content_reached_henry is False
