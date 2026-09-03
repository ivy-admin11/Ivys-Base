"""Structured delivery receipts and user-facing fallback helpers.

When a PDF attachment cannot be delivered, this module provides:
- AttachmentDeliveryReceipt — structured result (never a raw bool)
- build_attachment_failure_notice() — user-facing status message
- split_imessage_content() — split long text at paragraph/item boundaries
- format_happy_hour_text() — readable text fallback for Happy Hour reports
- format_meal_text() — readable text fallback for Familia meal plans

User-facing strings produced here must never contain local file paths,
stack traces, AppleScript errors, API keys, or internal exception text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence


# ---------------------------------------------------------------------------
# Delivery receipt
# ---------------------------------------------------------------------------

_UNVERIFIED_STATUSES = frozenset({"verified_delivered", "submitted_unverified"})


@dataclass
class AttachmentDeliveryReceipt:
    """Structured result of an iMessage attachment send attempt.

    Caller-visible fields
    ---------------------
    report_id   : str   — opaque reference for resend commands
    status      : str   — "verified_delivered" | "submitted_unverified" | "failed"
    file_size_bytes : int
    attempts    : int   — total send attempts made
    attempted_at: str   — ISO-8601 UTC
    verified_at : Optional[str] — set only for verified_delivered

    Internal-only fields (must not appear in user-facing messages)
    ---------------------------------------------------------------
    attachment_path, staged_path, applescript_result, error_code, error_detail
    """

    report_id: str
    status: str
    attachment_path: str
    staged_path: str
    file_size_bytes: int
    attempts: int
    applescript_result: str
    error_code: Optional[str] = None
    error_detail: Optional[str] = None
    attempted_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    verified_at: Optional[str] = None

    def __bool__(self) -> bool:
        """True for verified_delivered or submitted_unverified — not for failed."""
        return self.status in _UNVERIFIED_STATUSES

    @classmethod
    def make_failed(
        cls,
        report_id: str,
        attachment_path: str,
        staged_path: str,
        file_size_bytes: int,
        attempts: int,
        error_code: str,
        error_detail: str,
        applescript_result: str = "",
    ) -> "AttachmentDeliveryReceipt":
        return cls(
            report_id=report_id,
            status="failed",
            attachment_path=attachment_path,
            staged_path=staged_path,
            file_size_bytes=file_size_bytes,
            attempts=attempts,
            applescript_result=applescript_result,
            error_code=error_code,
            error_detail=error_detail,
        )

    @classmethod
    def make_unverified(
        cls,
        report_id: str,
        attachment_path: str,
        staged_path: str,
        file_size_bytes: int,
        attempts: int,
        applescript_result: str,
    ) -> "AttachmentDeliveryReceipt":
        return cls(
            report_id=report_id,
            status="submitted_unverified",
            attachment_path=attachment_path,
            staged_path=staged_path,
            file_size_bytes=file_size_bytes,
            attempts=attempts,
            applescript_result=applescript_result,
        )

    @classmethod
    def make_verified(
        cls,
        report_id: str,
        attachment_path: str,
        staged_path: str,
        file_size_bytes: int,
        attempts: int,
        applescript_result: str,
    ) -> "AttachmentDeliveryReceipt":
        return cls(
            report_id=report_id,
            status="verified_delivered",
            attachment_path=attachment_path,
            staged_path=staged_path,
            file_size_bytes=file_size_bytes,
            attempts=attempts,
            applescript_result=applescript_result,
            verified_at=datetime.now(timezone.utc).isoformat(),
        )


# ---------------------------------------------------------------------------
# User-facing failure notice (first fallback bubble)
# ---------------------------------------------------------------------------

def build_attachment_failure_notice(
    report_name: str,
    report_id: str,
    resend_command: str,
    retry_queued: bool = True,
) -> str:
    """Return the status message sent when PDF delivery explicitly fails.

    Never includes file paths, error details, or internal state.
    """
    retry_line = (
        "I saved the report and will retry automatically."
        if retry_queued
        else "I saved the report."
    )
    return "\n".join([
        f"⚠️ {report_name} PDF couldn't be sent.",
        "",
        retry_line,
        "",
        f'Reply "{resend_command}" to retry now.',
        f"Ref: {report_id}",
    ])


# ---------------------------------------------------------------------------
# iMessage content splitter
# ---------------------------------------------------------------------------

def split_imessage_content(text: str, max_chars: int = 1200) -> List[str]:
    """Split text into iMessage bubbles at paragraph or item boundaries.

    Never splits at an arbitrary character position; always breaks between
    paragraphs (double-newline-separated blocks) or between list items
    (lines starting with •, -, *, numbers). Targets 600–1200 chars per bubble.
    """
    if len(text) <= max_chars:
        return [text]

    # Split on paragraph boundaries first
    paragraphs = text.split("\n\n")

    bubbles: List[str] = []
    current_parts: List[str] = []
    current_len: int = 0

    for para in paragraphs:
        para_len = len(para) + 2  # +2 for the \n\n separator

        if current_len + para_len > max_chars and current_parts:
            bubbles.append("\n\n".join(current_parts))
            current_parts = [para]
            current_len = len(para) + 2
        else:
            current_parts.append(para)
            current_len += para_len

    if current_parts:
        bubbles.append("\n\n".join(current_parts))

    return bubbles or [text]


# ---------------------------------------------------------------------------
# Interactive report detail (backs the MORE / WHY <n> reply commands)
# ---------------------------------------------------------------------------

def build_detail(
    *,
    title: str,
    items: Sequence[Dict[str, Any]],
    shown: int = 0,
    more_intro: str = "",
) -> Dict[str, Any]:
    """Shape the per-item payload that powers MORE / WHY replies.

    ``items`` entries use two keys:
      headline — the one-line form of the item (what MORE sends)
      detail   — the reasoning/extra context (what WHY <n> sends)
    ``shown`` is how many items the main text already covered, so MORE knows
    where to pick up.
    """
    normalized: List[Dict[str, Any]] = []
    for i, item in enumerate(items, 1):
        normalized.append({
            "n": i,
            "headline": str(item.get("headline") or "").strip(),
            "detail": str(item.get("detail") or "").strip(),
        })
    return {
        "title": title,
        "shown": max(0, int(shown)),
        "more_intro": more_intro,
        "items": normalized,
    }


# ---------------------------------------------------------------------------
# Happy Hour text fallback
# ---------------------------------------------------------------------------

def format_happy_hour_text(discovery_data: dict) -> str:
    """Format the top five verified happy hour specials as a readable iMessage.

    Uses the structured discovery payload from fetch_local_specials(). Only
    includes specials where the data is present — never describes an
    unverified special as active.
    """
    specials = (discovery_data.get("specials") or [])[:5]
    venues = {v.get("name", ""): v for v in (discovery_data.get("venues") or [])}
    timestamp = datetime.now().strftime("%b %-d")

    lines = [f"🍹 Ivy's Happy Hour Scout — {timestamp}"]

    if not specials:
        lines.append("\nNo verified specials found for today.")
        return "\n".join(lines)

    lines.append("")
    for i, special in enumerate(specials, 1):
        venue = special.get("venue", "")
        detail = special.get("detail", "")
        if not venue or not detail:
            continue

        # Venue info
        venue_info = venues.get(venue, {})
        region = venue_info.get("region", "Frisco/Dallas, TX")

        # Build entry
        entry_lines = [f"{i}. {venue}"]
        entry_lines.append(f"   {detail}")

        days_hours = special.get("days_hours") or special.get("hours")
        if days_hours:
            entry_lines.append(f"   🕒 {days_hours}")

        entry_lines.append(f"   📍 {region}")
        lines.append("\n".join(entry_lines))

    return "\n\n".join(lines)


def build_happy_hour_report(discovery_data: dict, top_n: int = 3) -> tuple:
    """Return ``(body, detail)`` for the text-first Happy Hour report.

    The body names the top few verified specials; every special (with its
    hours and neighbourhood) is kept in the detail payload for MORE / WHY.
    """
    specials = discovery_data.get("specials") or []
    venues = {v.get("name", ""): v for v in (discovery_data.get("venues") or [])}
    timestamp = datetime.now().strftime("%b %-d")
    header = f"\U0001F379 Happy Hour Scout \u2014 {timestamp}"

    usable = [s for s in specials if s.get("venue") and s.get("detail")]
    if not usable:
        return f"{header}\n\nNo verified specials found today.", build_detail(
            title=header, items=[], shown=0,
        )

    def _hours(s):
        return s.get("days_hours") or s.get("hours") or ""

    def _region(s):
        return (venues.get(s.get("venue", ""), {}) or {}).get("region", "Frisco/Dallas, TX")

    def _headline(s, n):
        return f"{n}. {s['venue']}\n   {s['detail']}"

    def _detail(s, n):
        lines = [f"\U0001F50E #{n} \u2014 {s['venue']}", s["detail"]]
        if _hours(s):
            lines.append(f"\U0001F551 {_hours(s)}")
        lines.append(f"\U0001F4CD {_region(s)}")
        return "\n".join(lines)

    shown = min(top_n, len(usable))
    blocks = [f"{header}\n{len(usable)} verified special{'' if len(usable) == 1 else 's'}"]
    for i, s in enumerate(usable[:shown], 1):
        block = _headline(s, i)
        if _hours(s):
            block += f"\n   \U0001F551 {_hours(s)}"
        blocks.append(block)

    remaining = len(usable) - shown
    if remaining > 0:
        blocks.append(
            f"+{remaining} more spot{'' if remaining == 1 else 's'} \u2014 reply MORE"
        )

    detail = build_detail(
        title=header,
        items=[
            {"headline": _headline(s, i), "detail": _detail(s, i)}
            for i, s in enumerate(usable, 1)
        ],
        shown=shown,
        more_intro="\U0001F4CC The rest of tonight's list:",
    )
    return "\n\n".join(blocks), detail


def build_meal_report(meal_data: dict, top_n: int = 3) -> tuple:
    """Return ``(body, detail)`` for the text-first Familia meal plan.

    The body lists the first few dinners; the full week, with ingredients and
    the toddler adaptation, lives in the detail payload.
    """
    recipes = meal_data.get("recipes") or []
    timestamp = datetime.now().strftime("%b %-d")
    header = f"\U0001F37D\uFE0F Familia Meal Plan \u2014 week of {timestamp}"

    if not recipes:
        return f"{header}\n\nNo recipes in this plan.", build_detail(
            title=header, items=[], shown=0,
        )

    def _name(r, n):
        return r.get("recipe_name") or r.get("name") or f"Recipe {n}"

    def _minutes(r):
        return (r.get("prep_time_minutes") or 0) + (r.get("cooking_time_minutes") or 0)

    def _headline(r, n):
        meta = [x for x in (r.get("cuisine_origin") or r.get("cuisine") or "",
                            f"{_minutes(r)} min" if _minutes(r) else "") if x]
        line = f"{n}. {_name(r, n)}"
        if meta:
            line += "\n   " + " \u00b7 ".join(meta)
        return line

    def _detail(r, n):
        lines = [f"\U0001F50E #{n} \u2014 {_name(r, n)}"]
        meta = [x for x in (r.get("cuisine_origin") or r.get("cuisine") or "",
                            f"{_minutes(r)} min total" if _minutes(r) else "") if x]
        if meta:
            lines.append(" \u00b7 ".join(meta))
        ingredients = r.get("ingredients") or []
        if ingredients:
            lines.append("")
            lines.append("\U0001F6D2 " + ", ".join(str(i) for i in ingredients[:12]))
        adaptations = r.get("toddler_adaptations") or []
        if adaptations:
            lines.append("")
            lines.append("\U0001F476 " + ", ".join(str(a) for a in adaptations[:3]))
        macros = r.get("macros") or {}
        if macros:
            lines.append(
                "\U0001F4AA "
                + " \u00b7 ".join(f"{k.replace('_g', '')}: {v}g" for k, v in macros.items())
            )
        return "\n".join(lines)

    shown = min(top_n, len(recipes))
    blocks = [(
        f"{header}\n{len(recipes)} dinner{'' if len(recipes) == 1 else 's'} "
        f"\u00b7 Venezuelan-American-Asian \u00b7 toddler-friendly"
    )]
    for i, r in enumerate(recipes[:shown], 1):
        blocks.append(_headline(r, i))

    remaining = len(recipes) - shown
    if remaining > 0:
        blocks.append(
            f"+{remaining} more meal{'' if remaining == 1 else 's'} \u2014 reply MORE"
        )

    detail = build_detail(
        title=header,
        items=[
            {"headline": _headline(r, i), "detail": _detail(r, i)}
            for i, r in enumerate(recipes, 1)
        ],
        shown=shown,
        more_intro="\U0001F4CC The rest of the week:",
    )
    return "\n\n".join(blocks), detail


# ---------------------------------------------------------------------------
# Familia Meal Plan text fallback
# ---------------------------------------------------------------------------

def format_meal_text(meal_data: dict) -> str:
    """Format up to seven meal plan recipes as a readable iMessage.

    Includes cuisine, total time, and toddler adaptation for each recipe.
    Uses compress_meal_plan() structure as the base and augments it with
    per-recipe detail.
    """
    recipes = (meal_data.get("recipes") or [])[:7]
    timestamp = datetime.now().strftime("%b %-d")

    lines = [f"🍽️ Familia Meal Plan — Week of {timestamp}"]
    lines.append("Venezuelan-American-Asian Fusion | Toddler-friendly")

    if not recipes:
        lines.append("\nNo recipes in this plan.")
        return "\n".join(lines)

    lines.append("")
    for i, recipe in enumerate(recipes, 1):
        name = recipe.get("recipe_name") or recipe.get("name") or f"Recipe {i}"
        cuisine = recipe.get("cuisine_origin") or recipe.get("cuisine") or ""
        prep = recipe.get("prep_time_minutes") or 0
        cook = recipe.get("cooking_time_minutes") or 0
        total_time = prep + cook

        adaptations = recipe.get("toddler_adaptations") or []
        adapt_str = ", ".join(adaptations[:2]) if adaptations else "Family-friendly"

        entry = [f"{i}. {name}"]
        meta_parts = []
        if cuisine:
            meta_parts.append(cuisine)
        if total_time:
            meta_parts.append(f"{total_time} min")
        if meta_parts:
            entry.append(f"   {' · '.join(meta_parts)}")
        entry.append(f"   👶 {adapt_str}")
        lines.append("\n".join(entry))

    return "\n\n".join(lines)
