"""Text-first report delivery for every Ivy job.

Why this module exists
----------------------
Until 2026-09-03 every report job delivered a PDF attachment first and only
texted the content if ``send_imessage_attachment`` came back explicitly
``failed``. Two things made that lose reports silently:

1. ``AttachmentDeliveryReceipt.__bool__`` is True for ``submitted_unverified``
   — the state Messages leaves behind when AppleScript accepted the send but
   chat.db never confirmed the upload. The job then took the success branch,
   stamped the content fingerprint as "already reported", and never sent the
   text. Henry got nothing. The outbox metadata shows this was the *usual*
   outcome, not a rare one (SP-20260828-2107, SP-20260829-1502,
   SP-20260901-1525 — all ``submitted_unverified``, none delivered).
2. Even when the PDF did land, the content was locked inside an attachment
   that is awkward to read on a phone.

So the order is inverted here: **text is the delivery, the PDF is an archive.**
Every job sends its content as iMessage bubbles on every run. The PDF (for the
jobs that still build one) is copied into the outbox and sent only when Henry
asks for it by replying ``PDF`` / ``RESEND PICKS``.

Reports are also interactive: the last bubble carries a short command footer,
and the per-item detail behind a report is persisted alongside it so the
inbound poller can answer ``MORE`` and ``WHY 2`` without re-running the job.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Sequence

import ivy_core.outbox as _outbox
from ivy_core.messaging import send_imessage
from ivy_core.report_fallback import build_detail, split_imessage_content

logger = logging.getLogger("ivy.text_delivery")

# iMessage renders long bubbles fine, but ~1200 chars is the point where a
# bubble stops being skimmable on a phone. split_imessage_content only ever
# breaks between paragraphs/items, so bubbles stay semantically whole.
BUBBLE_MAX_CHARS = 1200

# Room reserved so the footer can ride along on the final bubble instead of
# arriving as a lonely extra message.
_FOOTER_HEADROOM = 220

DEFAULT_COMMANDS: Sequence[str] = ("MORE", "WHY <n>", "PDF")

# Re-exported so jobs have one import for "build the report, send the report".
__all__ = [
    "BUBBLE_MAX_CHARS",
    "DEFAULT_COMMANDS",
    "TextDeliveryResult",
    "build_detail",
    "build_footer",
    "deliver_report",
    "split_imessage_content",
]

# Delivery statuses written to outbox metadata.
STATUS_DELIVERED = "text_delivered"
STATUS_PARTIAL = "text_partial"
STATUS_FAILED = "text_failed"


@dataclass
class TextDeliveryResult:
    """Honest outcome of a text-first report send.

    ``delivered`` is True only when every bubble was accepted by Messages —
    there is no "probably sent" state here, which is the whole point.
    """

    report_id: str
    job_name: str
    status: str
    bubbles_sent: int
    bubbles_total: int
    pdf_archived: bool = False
    detail_saved: bool = False

    @property
    def delivered(self) -> bool:
        return self.status == STATUS_DELIVERED

    def __bool__(self) -> bool:
        return self.delivered


def build_footer(
    commands: Sequence[str] = DEFAULT_COMMANDS,
    *,
    report_id: Optional[str] = None,
) -> str:
    """Return the reply-command footer appended to the final bubble.

    Kept to one or two short lines — it is a prompt, not documentation.
    """
    cmds = [c for c in commands if c]
    if not cmds:
        return ""
    line = "Reply " + " · ".join(cmds)
    if report_id:
        return f"—\n{line}\nRef: {report_id}"
    return f"—\n{line}"


def deliver_report(
    phone: str,
    *,
    job_name: str,
    body: str,
    report_id: Optional[str] = None,
    detail: Optional[Dict[str, Any]] = None,
    pdf_path: Optional[str] = None,
    content_summary: str = "",
    commands: Sequence[str] = DEFAULT_COMMANDS,
    include_footer: bool = True,
    sender: Optional[Callable[[str, str], bool]] = None,
) -> TextDeliveryResult:
    """Send a report as iMessage text; archive the PDF without sending it.

    Never raises for archival problems — a failure to copy the PDF or write
    the detail payload must not stop the text from going out.

    ``sender`` is resolved at call time (not bound as a default) so tests — and
    any caller that needs a different transport — can substitute one.
    """
    send = sender or send_imessage
    report_id = report_id or _outbox.make_report_id(job_name)

    pdf_archived = False
    try:
        _outbox.save_report(
            report_id,
            pdf_path,
            job_name=job_name,
            recipient=phone,
            content_summary=content_summary,
            status="pending",
        )
        pdf_archived = bool(pdf_path)
    except Exception as exc:  # archival is best-effort
        logger.warning("Outbox archive skipped for %s: %s", report_id, exc)

    detail_saved = False
    if detail:
        try:
            _outbox.save_detail(report_id, job_name, detail)
            detail_saved = True
        except Exception as exc:
            logger.warning("Detail payload not saved for %s: %s", report_id, exc)

    bubbles = split_imessage_content(body, max_chars=BUBBLE_MAX_CHARS)
    if include_footer:
        footer = build_footer(commands, report_id=report_id)
        if footer:
            last = bubbles[-1]
            if len(last) + len(footer) + 2 <= BUBBLE_MAX_CHARS + _FOOTER_HEADROOM:
                bubbles[-1] = f"{last}\n\n{footer}"
            else:
                bubbles.append(footer)

    sent = 0
    for bubble in bubbles:
        if not send(phone, bubble):
            logger.error(
                "Text delivery stalled for %s at bubble %d/%d",
                report_id, sent + 1, len(bubbles),
            )
            break
        sent += 1

    if sent == len(bubbles):
        status = STATUS_DELIVERED
    elif sent > 0:
        status = STATUS_PARTIAL
    else:
        status = STATUS_FAILED

    try:
        _outbox.update_report_status(report_id, status, attempts=1)
    except Exception as exc:
        logger.warning("Outbox status not updated for %s: %s", report_id, exc)

    logger.info(
        "Report %s (%s) → %s (%d/%d bubbles, pdf_archived=%s)",
        report_id, job_name, status, sent, len(bubbles), pdf_archived,
    )
    return TextDeliveryResult(
        report_id=report_id,
        job_name=job_name,
        status=status,
        bubbles_sent=sent,
        bubbles_total=len(bubbles),
        pdf_archived=pdf_archived,
        detail_saved=detail_saved,
    )
