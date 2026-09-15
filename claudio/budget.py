"""Cumulative spend enforcement — a policy layer, not an estimate.

`--max-budget-usd` caps one call. That is useful but not what anyone
actually worries about: the real question is "how much has today cost me",
and no single-call cap can answer it. This module holds the running total
across calls and turns it into a hard stop.

This is only honest because the ledger records *billed* figures now (see
`usage.py`). A daily cap enforced against estimates would be enforcing a
number that reads low by orders of magnitude — worse than no cap, because
it would feel like protection while letting real spend through.

Two mechanisms, deliberately different in strength:

  - **Refuse.** Today's billed spend already meets or exceeds the cap, so
    the next call is blocked outright before anything is sent.
  - **Clamp.** Some budget is left, so the remainder is handed to the CLI
    as `--max-budget-usd`. The CLI then stops *mid-run* rather than
    overshooting — which a claudio-side check alone could never do, since
    claudio only regains control after the call is over.

Clamping is what makes the cap real. Without it a single call started at
$0.01 under the cap could still bill $5.

Estimated-basis entries still count toward the total. They understate, so
the cap triggers later than it should — erring toward letting work through
rather than blocking it on a number claudio isn't sure of.
"""

from __future__ import annotations

import os

from claudio.config import load_config
from claudio.usage import get_stats


def daily_cap() -> float | None:
    """Resolve the daily spend cap from env > config. None disables it.

    Non-positive or unparseable values disable rather than block every
    call — a typo in a config file should not brick the tool.
    """
    raw = os.environ.get("CLAUDIO_DAILY_BUDGET_USD")
    if raw is None or raw == "":
        raw = load_config().get("daily_budget_usd")
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def spent_today() -> float:
    """Billed + estimated spend recorded so far today."""
    return float(get_stats()["today"]["cost"])


def remaining_today() -> float | None:
    """Budget left today, or None when no cap is configured.

    Clamped at zero so callers never see a negative allowance.
    """
    cap = daily_cap()
    if cap is None:
        return None
    return max(0.0, cap - spent_today())


def check(requested_max: float | None = None) -> tuple[bool, float | None, str | None]:
    """Decide whether the next call may proceed, and under what ceiling.

    Args:
        requested_max: a per-call `--max-budget-usd` the user already asked
            for. The stricter of it and the daily remainder wins — an
            explicit per-call cap must never *raise* the daily ceiling.

    Returns:
        (allowed, effective_max_budget, message)

        `effective_max_budget` is what should be passed to the CLI, and is
        None when nothing constrains the call. `message` is a line worth
        showing the user (a refusal reason, or a notice that the daily cap
        is now the binding constraint); None when there's nothing to say.
    """
    cap = daily_cap()
    if cap is None:
        return True, requested_max, None

    spent = spent_today()
    left = max(0.0, cap - spent)

    if left <= 0:
        return False, None, (
            f"daily budget reached: ${spent:.4f} of ${cap:.2f} spent today. "
            f"Raise `daily_budget_usd`, set CLAUDIO_DAILY_BUDGET_USD, or wait "
            f"for tomorrow."
        )

    # The daily remainder binds unless the user asked for something tighter.
    if requested_max is None or left < requested_max:
        effective = left
        note = (f"daily budget: ${left:.4f} left of ${cap:.2f} "
                f"— capping this call there")
    else:
        effective = requested_max
        note = None

    return True, effective, note
