"""Text-first delivery and the interactive reply commands.

The bug these lock down: reports used to go out as a PDF attachment only, and
``submitted_unverified`` (AppleScript accepted it, chat.db never confirmed it)
counted as success — so the text fallback never ran and the report vanished.
Nothing here touches Messages.app, chat.db, or a real subprocess.
"""

import pytest

from ivy_core import outbox, text_delivery
from ivy_core.report_fallback import (
    build_detail,
    build_happy_hour_report,
    build_meal_report,
)


@pytest.fixture
def box(tmp_path, monkeypatch):
    """Point the outbox at a scratch directory."""
    monkeypatch.setattr(outbox, "OUTBOX_DIR", tmp_path / "outbox")
    return tmp_path


@pytest.fixture
def sent():
    return []


def _sender(sent, ok=True):
    def send(phone, body):
        sent.append((phone, body))
        return ok
    return send


# ---------------------------------------------------------------------------
# deliver_report
# ---------------------------------------------------------------------------

def test_text_is_sent_and_pdf_is_archived_not_pushed(box, sent):
    pdf = box / "report.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    result = text_delivery.deliver_report(
        "+15555550100",
        job_name="sharp_picks",
        body="Pick one\n\nPick two",
        detail=build_detail(title="T", items=[{"headline": "h", "detail": "d"}], shown=1),
        pdf_path=str(pdf),
        sender=_sender(sent),
    )

    assert result.delivered
    assert result.status == text_delivery.STATUS_DELIVERED
    assert sent, "nothing was texted"
    assert "Pick one" in "\n".join(b for _, b in sent)
    # The PDF exists in the outbox but was never handed to Messages.
    assert result.pdf_archived
    assert outbox.get_outbox_pdf_path(result.report_id) is not None
    assert outbox.load_report_meta(result.report_id)["status"] == "text_delivered"


def test_partial_send_is_not_reported_as_delivered(box, sent):
    calls = {"n": 0}

    def flaky(phone, body):
        calls["n"] += 1
        sent.append((phone, body))
        return calls["n"] == 1  # second bubble fails

    body = "\n\n".join(f"Item {i} " + "x" * 400 for i in range(6))
    result = text_delivery.deliver_report(
        "+15555550100", job_name="sharp_picks", body=body, sender=flaky,
    )

    assert result.bubbles_total > 1
    assert not result.delivered
    assert result.status == text_delivery.STATUS_PARTIAL
    assert bool(result) is False


def test_total_failure_is_status_failed(box, sent):
    result = text_delivery.deliver_report(
        "+15555550100", job_name="sharp_picks", body="anything",
        sender=_sender(sent, ok=False),
    )
    assert result.status == text_delivery.STATUS_FAILED
    assert not result.delivered


def test_footer_carries_the_reply_commands(box, sent):
    result = text_delivery.deliver_report(
        "+15555550100", job_name="sharp_picks", body="Short body",
        commands=("MORE", "WHY <n>", "PDF"), sender=_sender(sent),
    )
    last = sent[-1][1]
    assert "MORE" in last and "WHY <n>" in last and "PDF" in last
    assert result.report_id in last


def test_delivery_survives_a_missing_pdf(box, sent):
    """A broken PDF must cost the archive, never the message."""
    result = text_delivery.deliver_report(
        "+15555550100", job_name="sharp_picks", body="The picks",
        pdf_path=None, sender=_sender(sent),
    )
    assert result.delivered
    assert result.pdf_archived is False
    assert outbox.get_outbox_pdf_path(result.report_id) is None


def test_detail_payload_round_trips(box, sent):
    detail = build_detail(
        title="Sharp Picks",
        items=[{"headline": f"{i}. pick", "detail": f"why {i}"} for i in range(1, 8)],
        shown=3,
    )
    result = text_delivery.deliver_report(
        "+15555550100", job_name="sharp_picks", body="body",
        detail=detail, sender=_sender(sent),
    )
    loaded = outbox.load_detail(result.report_id)
    assert loaded["shown"] == 3
    assert len(loaded["items"]) == 7
    assert loaded["items"][6]["detail"] == "why 7"
    assert loaded["job_name"] == "sharp_picks"


def test_detail_files_do_not_pollute_report_lookups(box, sent):
    result = text_delivery.deliver_report(
        "+15555550100", job_name="sharp_picks", body="body",
        detail=build_detail(title="t", items=[{"headline": "h", "detail": "d"}], shown=1),
        sender=_sender(sent),
    )
    assert outbox.find_newest("sharp_picks") == result.report_id
    assert outbox.find_newest("sharp_picks", with_detail=True) == result.report_id
    assert outbox.find_newest("happy_hour") is None


# ---------------------------------------------------------------------------
# Job digests
# ---------------------------------------------------------------------------

def test_happy_hour_digest_holds_back_the_tail():
    data = {
        "venues": [{"name": f"Bar {i}", "region": "Frisco, TX"} for i in range(6)],
        "specials": [
            {"venue": f"Bar {i}", "detail": f"deal {i}", "days_hours": "3-6"}
            for i in range(6)
        ],
    }
    body, detail = build_happy_hour_report(data)
    assert "reply MORE" in body
    assert len(detail["items"]) == 6
    assert detail["shown"] == 3


def test_meal_digest_keeps_every_recipe_reachable():
    data = {"recipes": [
        {"recipe_name": f"Dish {i}", "cuisine_origin": "Venezuelan",
         "prep_time_minutes": 10, "cooking_time_minutes": 20,
         "ingredients": ["arepa flour", "chicken"], "toddler_adaptations": ["shred it"]}
        for i in range(5)
    ]}
    body, detail = build_meal_report(data)
    assert "Dish 0" in body
    assert len(detail["items"]) == 5
    assert "arepa flour" in detail["items"][4]["detail"]


def test_empty_reports_still_produce_a_message():
    body, detail = build_happy_hour_report({"specials": [], "venues": []})
    assert body.strip()
    assert detail["items"] == []
    body, detail = build_meal_report({"recipes": []})
    assert body.strip()
    assert detail["items"] == []


# ---------------------------------------------------------------------------
# Interactive reply commands (MORE / WHY <n> / PDF)
# ---------------------------------------------------------------------------

@pytest.fixture
def seeded(box, monkeypatch):
    """A delivered picks report with 7 picks, 3 of them shown."""
    import main

    monkeypatch.setattr(main._outbox, "OUTBOX_DIR", outbox.OUTBOX_DIR)
    detail = build_detail(
        title="Sharp Picks",
        items=[{"headline": f"{i}. Game {i}", "detail": f"Reasoning for {i}"}
               for i in range(1, 8)],
        shown=3,
        more_intro="The rest:",
    )
    result = text_delivery.deliver_report(
        "+15555550100", job_name="sharp_picks", body="digest",
        detail=detail, sender=lambda p, b: True,
    )
    return main, result.report_id


def test_more_sends_only_the_held_back_items(seeded):
    main, _report_id = seeded
    reply = main.handle_report_command("MORE", "+15555550100")
    assert reply is not None
    text = "\n".join(reply)
    assert "4. Game 4" in text and "7. Game 7" in text
    assert "1. Game 1" not in text, "MORE must not repeat what was already sent"


def test_more_is_case_and_target_insensitive(seeded):
    main, _ = seeded
    for cmd in ("more", "More picks", "MORE SHARP PICKS"):
        assert main.handle_report_command(cmd, "+15555550100") is not None


def test_why_returns_that_items_reasoning(seeded):
    main, _ = seeded
    reply = main.handle_report_command("WHY 5", "+15555550100")
    assert "Reasoning for 5" in "\n".join(reply)


def test_why_out_of_range_is_a_helpful_answer_not_a_crash(seeded):
    main, _ = seeded
    reply = main.handle_report_command("WHY 99", "+15555550100")
    assert "no #99" in "\n".join(reply).lower()


def test_pdf_request_explains_when_the_report_was_text_only(seeded):
    main, report_id = seeded
    reply = main.handle_report_command("PDF", "+15555550100")
    text = "\n".join(reply)
    assert report_id in text
    assert "MORE" in text, "a text-only report should point back at MORE"


def test_pdf_reports_unverified_sends_honestly(box, monkeypatch):
    """The exact state that used to be logged as success."""
    import main

    monkeypatch.setattr(main._outbox, "OUTBOX_DIR", outbox.OUTBOX_DIR)
    pdf = box / "r.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    result = text_delivery.deliver_report(
        "+15555550100", job_name="sharp_picks", body="digest",
        pdf_path=str(pdf), sender=lambda p, b: True,
    )

    from ivy_core.report_fallback import AttachmentDeliveryReceipt

    monkeypatch.setattr(
        main, "send_imessage_attachment",
        lambda phone, path, **k: AttachmentDeliveryReceipt.make_unverified(
            report_id=result.report_id, attachment_path=path, staged_path=path,
            file_size_bytes=12, attempts=1, applescript_result="SUCCESS",
        ),
    )

    reply = "\n".join(main.handle_report_command("PDF", "+15555550100"))
    assert "couldn't confirm" in reply
    assert "✅" not in reply, "an unconfirmed send must not be reported as delivered"


def test_non_commands_fall_through_to_the_llm(seeded):
    main, _ = seeded
    for text in ("why did you pick the Yankees?", "more info about tomorrow", "hey ivy"):
        assert main.handle_report_command(text, "+15555550100") is None


def test_commands_with_no_report_say_so(box, monkeypatch):
    import main

    monkeypatch.setattr(main._outbox, "OUTBOX_DIR", outbox.OUTBOX_DIR)
    reply = "\n".join(main.handle_report_command("MORE", "+15555550100"))
    assert "recently" in reply.lower() or "nothing" in reply.lower()
