"""Background PyPI update check.

Queries https://pypi.org/pypi/claudio-cli/json at most once per 24h and
caches the result. The actual fetch runs in a daemon thread so REPL
startup never blocks on the network. Notice rendering is cache-only —
if the fetch hasn't finished yet, the user simply doesn't see a notice
this run, and the next launch will find the cache populated.

Opt-out:
  - $CLAUDIO_NO_UPDATE_CHECK=1
  - $CI is set (CI runners shouldn't nag)
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from pathlib import Path

from claudio import __version__
from claudio.utils.colors import CYAN, DIM, YELLOW, colored


PYPI_URL = "https://pypi.org/pypi/claudio-cli/json"
CHECK_INTERVAL_SECONDS = 24 * 60 * 60  # 24h
FETCH_TIMEOUT_SECONDS = 3.0


def _cache_path() -> Path:
    base = os.environ.get("CLAUDIO_HOME")
    root = Path(base) if base else (Path.home() / ".claudio")
    return root / "update_check.json"


def _disabled() -> bool:
    if os.environ.get("CLAUDIO_NO_UPDATE_CHECK"):
        return True
    if os.environ.get("CI"):
        return True
    return False


def parse_version(v: str) -> tuple[int, ...]:
    """Parse a version into a comparable int-tuple.

    Tolerant of trailing non-numerics ("1.2.0rc1" -> (1, 2, 0)) and
    missing parts ("1.2" -> (1, 2)). Good enough for "is X newer than Y"
    on regular release versions; prereleases compare equal to the same
    base release, which biases away from nagging users to install a beta.
    """
    parts: list[int] = []
    for chunk in v.strip().split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) if parts else (0,)


def is_newer(remote: str, local: str) -> bool:
    """Return True iff remote release is strictly newer than local."""
    try:
        return parse_version(remote) > parse_version(local)
    except Exception:
        return False


def read_cache() -> dict | None:
    path = _cache_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_cache(latest: str, checked_at: float | None = None) -> None:
    path = _cache_path()
    # `is None` not `or` — 0 is a valid timestamp meaning "ages ago" and
    # tests rely on being able to write a stale cache explicitly.
    if checked_at is None:
        checked_at = time.time()
    payload = {"latest": latest, "checked_at": checked_at}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        pass


def fetch_latest_pypi(timeout: float = FETCH_TIMEOUT_SECONDS) -> str | None:
    """One-shot HTTP GET against PyPI. Returns the latest release string,
    or None on any failure (network, parse, etc.). Never raises."""
    try:
        req = urllib.request.Request(
            PYPI_URL,
            headers={"User-Agent": f"claudio-cli/{__version__}"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        info = data.get("info") or {}
        latest = info.get("version")
        if isinstance(latest, str) and latest:
            return latest
    except Exception:
        return None
    return None


def _cache_is_fresh(cache: dict, now: float | None = None) -> bool:
    checked = cache.get("checked_at", 0)
    now = now if now is not None else time.time()
    try:
        return (now - float(checked)) < CHECK_INTERVAL_SECONDS
    except (TypeError, ValueError):
        return False


def start_background_check() -> threading.Thread | None:
    """Launch the PyPI fetch in a daemon thread if the cache is stale.

    No-op when disabled or when the cache was refreshed within the last
    24 hours. The returned thread (if any) is intentionally not joined —
    the next process launch reads whatever it wrote.
    """
    if _disabled():
        return None
    cache = read_cache()
    if cache and _cache_is_fresh(cache):
        return None

    def _worker():
        latest = fetch_latest_pypi()
        if latest:
            write_cache(latest)

    t = threading.Thread(target=_worker, daemon=True, name="claudio-update-check")
    t.start()
    return t


def pending_notice(current: str = __version__, stream=None) -> str | None:
    """Return a one-line "new version" notice from cache, or None.

    Cache-only — never touches the network. Safe to call on every command.
    """
    if _disabled():
        return None
    cache = read_cache()
    if not cache:
        return None
    latest = cache.get("latest")
    if not isinstance(latest, str) or not latest:
        return None
    if not is_newer(latest, current):
        return None

    label = colored("[notice]", YELLOW, stream=stream, bold=True)
    arrow = colored("->", DIM, stream=stream)
    new_v = colored(latest, CYAN, stream=stream, bold=True)
    return (
        f"{label} A new release of claudio-cli is available: "
        f"{current} {arrow} {new_v}\n"
        f"        To update, run: pip install --upgrade claudio-cli"
    )
