"""Grocery staging tool (confirmation-gated).

Actual cart staging is performed by the async ``/stage_groceries`` endpoint,
which owns the shared Playwright browser held in ``app.state`` and enforces the
hard "never checkout" guardrail. Driving that browser synchronously from the
agent's background worker thread would be fragile and unsafe, so this tool is a
**confirmation stub**: it advertises the capability, is flagged
``requires_confirmation=True``, and returns a message directing the flow to the
secured endpoint rather than automating a purchase without explicit approval.
"""

from __future__ import annotations

import logging
from typing import List, Type

from pydantic import BaseModel, Field

from .base import BaseIvyTool

logger = logging.getLogger("ivy.tools.grocery")

ALLOWED_STORES = {"HEB", "KROGER"}


class GroceryArgs(BaseModel):
    store: str = Field(description="Store to stage the cart at: 'HEB' or 'Kroger'.")
    ingredients: List[str] = Field(
        description="List of grocery items to add to the cart."
    )


class StageGroceriesTool(BaseIvyTool):
    name: str = "stage_groceries"
    description: str = (
        "Stages a grocery cart at H-E-B or Kroger (a human always checks out). "
        "This action requires explicit user confirmation before it runs."
    )
    args_schema: Type[BaseModel] = GroceryArgs
    requires_confirmation: bool = True

    def _run(self, store: str, ingredients: List[str], **_: object) -> str:
        store_norm = (store or "").strip().upper()
        if store_norm not in ALLOWED_STORES:
            return (
                f"I can only stage carts at H-E-B or Kroger, not '{store}'."
            )
        count = len([i for i in ingredients if i and i.strip()])
        logger.info(
            "stage_groceries requested (confirmation required) | store=%s items=%s",
            store_norm,
            count,
        )
        store_label = "H-E-B" if store_norm == "HEB" else "Kroger"
        return (
            f"Staging a cart of {count} item(s) at {store_label} needs your "
            "confirmation first. Reply to confirm and I'll stage it via the secure "
            "checkout-blocked flow — you'll always complete checkout yourself."
        )
