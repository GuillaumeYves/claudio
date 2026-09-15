"""Usage tracking -- know what you're spending.

Stores per-request usage in ~/.config/claudio/usage.json.
Data is append-only (entries list) with periodic compaction.

Each entry:
  - ts: unix timestamp
  - cmd: command name (build, ask, run)
  - mode: submode (refactor, generate, review, question, debug)
  - input_tokens: input tokens (cache reads/writes included when billed)
  - output_tokens: output tokens
  - cost: cost in USD
  - cached: whether response came from claudio's own response cache
  - basis: "billed" when the figures come from the CLI's own usage report,
    "estimated" when they're local token estimates. Entries written before
    2.0.0 have no basis key and are treated as estimated.
  - cache_read_tokens / cache_creation_tokens: prompt-cache traffic, present
    on billed entries only

Why the basis matters: claudio's local estimate only sees the prompt claudio
composed. The real request also carries Claude Code's system prompt, the
project's CLAUDE.md, tool definitions and prompt-cache traffic. Billed entries
are the truth; estimated ones can be off by orders of magnitude on both tokens
and dollars, and `stats` says which it is showing.
"""

import json
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from claudio.utils.tokens import estimate_cost

if TYPE_CHECKING:  # avoid importing the executor at runtime
    from claudio.executor import CallUsage

USAGE_FILE = Path.home() / ".config" / "claudio" / "usage.json"


def log_request(
    cmd: str,
    mode: str,
    input_tokens: int,
    output_tokens: int = 500,
    cached: bool = False,
    model: str | None = None,
    usage: "CallUsage | None" = None,
) -> None:
    """Log a single request to usage history.

    When `usage` is given it wins outright: those are the CLI's own billed
    figures, and they supersede every local estimate — token counts, the
    dollar amount, and even the model (the CLI reports which model actually
    served the call, which may differ from the alias claudio asked for after
    a --fallback-model hop).

    Without it, `model` prices the entry at the right tier so `claudio stats`
    reflects what an Opus-floored build costs rather than assuming Sonnet.
    """
    if usage is not None and not cached:
        entry = {
            "ts": time.time(),
            "cmd": cmd,
            "mode": mode,
            "model": usage.model or model,
            "input_tokens": usage.billed_input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_tokens": usage.cache_read_tokens,
            "cache_creation_tokens": usage.cache_creation_tokens,
            # 8dp, not the 6dp used for estimates: billed figures routinely
            # carry 7 significant decimals (e.g. 0.0172027) and rounding an
            # exact number is the one thing this entry exists to avoid.
            "cost": round(usage.cost_usd, 8),
            "cached": False,
            "basis": "billed",
        }
    else:
        cost = 0.0 if cached else estimate_cost(input_tokens, output_tokens, model=model)
        entry = {
            "ts": time.time(),
            "cmd": cmd,
            "mode": mode,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost": round(cost, 6),
            "cached": cached,
            "basis": "estimated",
        }

    data = _load()
    data["entries"].append(entry)
    _save(data)


def get_stats() -> dict:
    """Compute usage statistics.

    Returns dict with keys: today, week, all_time, by_command.
    Each contains: requests, tokens_in, tokens_out, cost, cache_hits.
    """
    data = _load()
    entries = data.get("entries", [])

    now = time.time()
    today_start = _start_of_day(now)
    week_start = today_start - (6 * 86400)  # 7 days including today

    stats = {
        "today": _empty_stats(),
        "week": _empty_stats(),
        "all_time": _empty_stats(),
        "by_command": {},
        "by_model": {},
    }

    for e in entries:
        ts = e.get("ts", 0)
        cmd = e.get("cmd", "unknown")
        mode = e.get("mode", "")

        # All time
        _accumulate(stats["all_time"], e)

        # This week
        if ts >= week_start:
            _accumulate(stats["week"], e)

        # Today
        if ts >= today_start:
            _accumulate(stats["today"], e)

        # By command
        key = f"{cmd} -{mode}" if mode else cmd
        if key not in stats["by_command"]:
            stats["by_command"][key] = _empty_stats()
        _accumulate(stats["by_command"][key], e)

        # By model — the single biggest cost lever, so it deserves its own
        # axis rather than being inferable from the command breakdown.
        model = e.get("model") or "unspecified"
        if model not in stats["by_model"]:
            stats["by_model"][model] = _empty_stats()
        _accumulate(stats["by_model"][model], e)

    return stats


def costliest(limit: int = 5) -> list[dict]:
    """The most expensive individual requests, priciest first.

    Only meaningful now that entries carry billed figures — ranking
    estimates would rank the estimator's guesses, not real spend. Cache
    hits are excluded: they cost nothing and would only dilute the list.
    """
    entries = [e for e in _load().get("entries", []) if not e.get("cached")]
    entries.sort(key=lambda e: e.get("cost", 0.0), reverse=True)
    return entries[:limit]


def explain_cost(entry: dict) -> str:
    """One-line reason an individual request cost what it did.

    Mechanical attribution from what the entry records — model tier, how
    much of the input was uncached, and output volume. No guessing.
    """
    reasons = []
    model = (entry.get("model") or "").lower()
    if "opus" in model or "fable" in model:
        reasons.append(f"{model.split('-')[1] if '-' in model else model} rates")
    tokens_in = entry.get("input_tokens", 0)
    cached = entry.get("cache_read_tokens", 0)
    fresh = tokens_in - cached
    if fresh > 20_000:
        reasons.append(f"{fresh:,} uncached input tokens")
    elif tokens_in > 20_000:
        reasons.append(f"{tokens_in:,} input tokens")
    if entry.get("output_tokens", 0) > 4_000:
        reasons.append(f"{entry['output_tokens']:,} output tokens")
    if entry.get("basis") != "billed":
        reasons.append("estimated, not billed")
    return ", ".join(reasons) or "no single dominant factor"


def reset_stats() -> int:
    """Clear all usage data. Returns number of entries cleared."""
    data = _load()
    count = len(data.get("entries", []))
    _save({"entries": []})
    return count


def _empty_stats() -> dict:
    return {
        "requests": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "cost": 0.0,
        "cache_hits": 0,
        # How many of these requests carry the CLI's billed figures vs a
        # local estimate, so the display can label the total honestly.
        "billed_requests": 0,
        "estimated_requests": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
    }


def _accumulate(stats: dict, entry: dict) -> None:
    stats["requests"] += 1
    stats["tokens_in"] += entry.get("input_tokens", 0)
    stats["tokens_out"] += entry.get("output_tokens", 0)
    stats["cost"] += entry.get("cost", 0.0)
    stats["cache_read_tokens"] += entry.get("cache_read_tokens", 0)
    stats["cache_creation_tokens"] += entry.get("cache_creation_tokens", 0)
    if entry.get("cached"):
        stats["cache_hits"] += 1
    # Entries written before billed figures existed have no basis key.
    if entry.get("basis") == "billed":
        stats["billed_requests"] += 1
    else:
        stats["estimated_requests"] += 1


def _start_of_day(ts: float) -> float:
    dt = datetime.fromtimestamp(ts)
    start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.timestamp()


def _load() -> dict:
    if not USAGE_FILE.exists():
        return {"entries": []}
    try:
        return json.loads(USAGE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"entries": []}


def _save(data: dict) -> None:
    USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    USAGE_FILE.write_text(json.dumps(data), encoding="utf-8")
