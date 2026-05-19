"""Response cache -- avoid paying for the same prompt twice.

Cache design:
  - Location: .claudio/cache/ in the current workspace
  - Key: SHA-256 of the final prompt (after pipeline processing)
  - Value: JSON with response text, timestamp, token estimate
  - TTL: 1 hour default (configurable)
  - .claudio/ is auto-added to .gitignore

Cache is workspace-local because the same file path in different
projects points to different code. Global cache would serve stale
answers across projects.
"""

import hashlib
import json
import time
from pathlib import Path

CACHE_DIR = Path(".claudio") / "cache"
DEFAULT_TTL = 3600  # 1 hour

# Tracks whether the most recent cache_get returned a hit. The REPL reads
# this after each dispatch to decide whether session_turn should advance:
# a cached response means Claude was never actually called, so the next
# turn must still use --session-id (not --resume) or Claude will reject
# the conversation as unknown. Module-level state is the lightest plumbing
# across the long `repl → cli → command → run_prompt → cache` call chain.
_last_was_hit = False


def consume_last_hit() -> bool:
    """Return whether the last cache_get returned a hit, then reset.

    Read-and-clear semantics so a single hit signal can't be observed by
    more than one caller. The REPL is the intended consumer.
    """
    global _last_was_hit
    was_hit = _last_was_hit
    _last_was_hit = False
    return was_hit


def cache_key(prompt: str) -> str:
    """Generate a deterministic cache key from a prompt."""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]


def cache_get(prompt: str, ttl: int = DEFAULT_TTL) -> str | None:
    """Look up a cached response for a prompt.

    Returns the cached response string, or None on miss/expiry. Sets the
    module-level `_last_was_hit` flag so callers further up the chain
    (REPL) can detect cache hits without changing the return type.
    """
    global _last_was_hit
    _last_was_hit = False  # default: miss

    key = cache_key(prompt)
    path = CACHE_DIR / f"{key}.json"

    if not path.exists():
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    # Check TTL
    cached_at = data.get("ts", 0)
    if time.time() - cached_at > ttl:
        # Expired -- clean up
        try:
            path.unlink()
        except OSError:
            pass
        return None

    response = data.get("response")
    if response is not None:
        _last_was_hit = True
    return response


def cache_put(prompt: str, response: str, input_tokens: int = 0) -> None:
    """Store a response in the cache."""
    global _last_was_hit
    _last_was_hit = False  # real call, not a hit
    _ensure_cache_dir()

    key = cache_key(prompt)
    path = CACHE_DIR / f"{key}.json"

    data = {
        "ts": time.time(),
        "response": response,
        "input_tokens": input_tokens,
        "prompt_hash": key,
    }

    try:
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass  # Cache write failure is non-fatal


def cache_clear() -> int:
    """Clear all cached responses. Returns count of entries removed."""
    if not CACHE_DIR.exists():
        return 0

    count = 0
    for f in CACHE_DIR.glob("*.json"):
        try:
            f.unlink()
            count += 1
        except OSError:
            pass
    return count


def _ensure_cache_dir() -> None:
    """Create cache directory and ensure .claudio is gitignored."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # Auto-add .claudio/ to .gitignore if it exists
    gitignore = Path(".gitignore")
    marker = ".claudio/"

    if gitignore.exists():
        content = gitignore.read_text(encoding="utf-8", errors="replace")
        if marker not in content:
            with open(gitignore, "a", encoding="utf-8") as f:
                f.write(f"\n# Claudio local cache\n{marker}\n")
    else:
        # Don't create .gitignore if it doesn't exist -- not our repo
        pass
