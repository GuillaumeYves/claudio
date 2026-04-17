"""Usage tracking -- know what you're spending.

Stores per-request usage in ~/.config/claudio/usage.json.
Data is append-only (entries list) with periodic compaction.

Each entry:
  - ts: unix timestamp
  - cmd: command name (build, ask, run)
  - mode: submode (refactor, generate, review, question, debug)
  - input_tokens: estimated input tokens
  - output_tokens: estimated output tokens
  - cost: estimated cost in USD
  - cached: whether response came from cache
"""

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

from claudio.utils.tokens import estimate_cost

USAGE_FILE = Path.home() / ".config" / "claudio" / "usage.json"


def log_request(
    cmd: str,
    mode: str,
    input_tokens: int,
    output_tokens: int = 500,
    cached: bool = False,
) -> None:
    """Log a single request to usage history."""
    cost = 0.0 if cached else estimate_cost(input_tokens, output_tokens)

    entry = {
        "ts": time.time(),
        "cmd": cmd,
        "mode": mode,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost": round(cost, 6),
        "cached": cached,
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

    return stats


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
    }


def _accumulate(stats: dict, entry: dict) -> None:
    stats["requests"] += 1
    stats["tokens_in"] += entry.get("input_tokens", 0)
    stats["tokens_out"] += entry.get("output_tokens", 0)
    stats["cost"] += entry.get("cost", 0.0)
    if entry.get("cached"):
        stats["cache_hits"] += 1


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
