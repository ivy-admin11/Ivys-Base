"""Small helpers for proactive-agent delivery result contracts.

These helpers do not send anything.  They only normalize already-observed
results so an agent can describe attempted and intentionally skipped delivery
without requiring the detached worker to infer semantics from a bare boolean.
"""

from __future__ import annotations

from typing import Any, Iterable


DELIVERY_STATUSES = frozenset({
    "not_requested",
    "not_attempted",
    "submitted_unverified",
    "verified_delivered",
    "failed",
    "partial",
    "unknown",
})

_POSITIVE_STATUSES = frozenset({"submitted_unverified", "verified_delivered"})


def text_delivery_status(submitted: Any) -> str:
    """Map a text sender's boolean result to a delivery status."""
    return "submitted_unverified" if submitted is True else "failed"


def attachment_delivery_status(receipt: Any) -> str:
    """Return a structured attachment receipt's status conservatively."""
    status = getattr(receipt, "status", None)
    if isinstance(status, str) and status in DELIVERY_STATUSES:
        return status
    return "submitted_unverified" if bool(receipt) else "failed"


def aggregate_delivery_status(statuses: Iterable[str]) -> str:
    """Aggregate known per-recipient report-delivery outcomes.

    An empty set represents a legitimate no-send, not an unknown attempt.
    Failure notices are deliberately excluded by callers because submitting a
    notice does not mean the requested report itself was delivered.
    """
    values = [status for status in statuses if status in DELIVERY_STATUSES]
    if not values:
        return "not_attempted"
    if all(status == "not_requested" for status in values):
        return "not_requested"
    if all(status == "not_attempted" for status in values):
        return "not_attempted"
    if all(status == "verified_delivered" for status in values):
        return "verified_delivered"
    if all(status in _POSITIVE_STATUSES for status in values):
        return "submitted_unverified"
    if any(status == "partial" for status in values):
        return "partial"
    if any(status in _POSITIVE_STATUSES for status in values):
        return "partial"
    if any(status == "unknown" for status in values):
        return "unknown"
    if any(status == "failed" for status in values):
        return "failed"
    return "not_attempted"


def is_delivery_submitted(status: str) -> bool:
    """Whether at least some requested report content was submitted."""
    return status in _POSITIVE_STATUSES or status == "partial"
