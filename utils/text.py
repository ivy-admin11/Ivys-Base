"""Text/payload helpers shared across tools."""

from __future__ import annotations

import logging

logger = logging.getLogger("ivy.text")


def optimize_token_payload(raw_text: str, max_chars: int = 3500) -> str:
    """Enforce token constraints by trimming blank lines and hard-truncating.

    Keeps payloads sent to the LLM bounded so a huge Readwise export or
    calendar dump cannot blow the context window / token budget.
    """
    if not raw_text:
        return "Empty payload dataset."
    lines = [line.strip() for line in raw_text.split("\n") if line.strip()]
    cleaned_text = "\n".join(lines)
    if len(cleaned_text) <= max_chars:
        return cleaned_text
    logger.warning(
        "Payload size alert (%d chars). Token optimization chunking engaged.",
        len(cleaned_text),
    )
    return (
        cleaned_text[:max_chars]
        + "\n\n[...System Truncation applied for strict Token optimization...]"
    )
