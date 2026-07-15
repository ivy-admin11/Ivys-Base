"""Ad-hoc sharp-picks trigger: lets Ivy run the sports-betting picks on demand."""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Type

from pydantic import BaseModel, Field

from .base import BaseIvyTool

logger = logging.getLogger("ivy.tools.sports")

PROJECT_ROOT = "/Users/lexi/openclaw-admin"
PICKS_SCRIPT = os.path.join(PROJECT_ROOT, "run_daily_picks.sh")


def run_sharp_picks() -> str:
    """Launch the sharp-picks pipeline in the background, forcing a fresh send.

    The pipeline sweeps the curated X/Grok handicapper accounts, so it takes a
    few minutes; it is launched detached and texts Henry when done. SPORTS_FORCE_SEND
    bypasses the daily duplicate-suppression so an on-demand request always delivers.
    """
    if not os.path.exists(PICKS_SCRIPT):
        return "❌ Sharp-picks script not found on disk."
    env = dict(os.environ)
    env["SPORTS_FORCE_SEND"] = "1"
    try:
        subprocess.Popen(
            ["/bin/bash", PICKS_SCRIPT],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("run_sharp_picks failed to launch: %s", exc)
        return f"❌ Could not start sharp picks: {exc}"
    return (
        "🔒 Running Ivy's Sharp Picks now — sweeping the X handicapper accounts "
        "across MLB, NFL, NBA, NHL, Soccer, KBO, World Cup, PGA golf, and Tennis. You'll get the "
        "picks with confidence scores by text in a few minutes."
    )


class RunSharpPicksArgs(BaseModel):
    reason: str = Field(
        default="",
        description="Optional note on why the picks were requested.",
    )


class RunSharpPicksTool(BaseIvyTool):
    name: str = "run_sharp_picks"
    description: str = (
        "Generate and text Henry's sharp sports-betting picks on demand (ad-hoc), "
        "outside the normal schedule. Each pick includes a confidence score based on "
        "the X/Grok handicapper accounts and covers MLB, NFL, NBA, Soccer (any league), "
        "KBO, World Cup, PGA golf, NHL, and Tennis. Use whenever Henry asks for picks, sharp picks, "
        "or to refresh/re-run them."
    )
    args_schema: Type[BaseModel] = RunSharpPicksArgs
    requires_confirmation: bool = False

    def _run(self, reason: str = "", **_: object) -> str:
        return run_sharp_picks()
