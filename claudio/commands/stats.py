"""claudio stats -- view token usage and cost tracking.

Usage:
    claudio stats               Show usage summary
    claudio stats --reset       Clear all usage data
    claudio stats --json        Output as JSON
"""

import json

from claudio.cache import cache_clear
from claudio.usage import costliest, explain_cost, get_stats, reset_stats


def execute(raw_args: list[str], ctx: dict) -> int:
    # Handle --reset
    if "--reset" in raw_args:
        count = reset_stats()
        cache_count = cache_clear()
        print(f"Cleared {count} usage entries and {cache_count} cached responses.")
        return 0

    stats = get_stats()

    if ctx.get("json_output"):
        print(json.dumps(stats, indent=2))
        return 0

    _print_stats(stats)
    return 0


def _print_stats(stats: dict) -> None:
    print("Claudio Usage Stats\n")

    # Summary table
    print(f"  {'Period':<12} {'Requests':>9} {'Tokens In':>11} {'Cost':>10} {'Cache Hits':>11}")
    print(f"  {'-'*12} {'-'*9} {'-'*11} {'-'*10} {'-'*11}")

    for label, key in [("Today", "today"), ("This week", "week"), ("All time", "all_time")]:
        s = stats[key]
        cost_str = f"${s['cost']:.4f}" if s["cost"] > 0 else "$0"
        print(
            f"  {label:<12} {s['requests']:>9,} {s['tokens_in']:>11,} {cost_str:>10} {s['cache_hits']:>11,}"
        )

    # Per-model breakdown. Model choice is the single biggest cost lever,
    # so it gets its own axis — "opus is 80% of my bill" is the actionable
    # fact, and it is invisible in a per-command view.
    by_model = stats.get("by_model", {})
    if len(by_model) > 1:
        total = sum(m["cost"] for m in by_model.values()) or 1.0
        print("\n  By Model:")
        print(f"  {'Model':<26} {'Requests':>9} {'Cost':>10} {'Share':>7}")
        print(f"  {'-'*26} {'-'*9} {'-'*10} {'-'*7}")
        for name, m in sorted(by_model.items(), key=lambda x: x[1]["cost"],
                              reverse=True):
            share = m["cost"] / total * 100
            print(f"  {name[:26]:<26} {m['requests']:>9,} "
                  f"${m['cost']:>9.4f} {share:>6.0f}%")

    # Per-command breakdown
    by_cmd = stats.get("by_command", {})
    if by_cmd:
        print("\n  By Command:")
        print(f"  {'Command':<22} {'Requests':>9} {'Tokens In':>11} {'Cost':>10}")
        print(f"  {'-'*22} {'-'*9} {'-'*11} {'-'*10}")

        # Sort by cost descending
        for cmd, s in sorted(by_cmd.items(), key=lambda x: x[1]["cost"], reverse=True):
            cost_str = f"${s['cost']:.4f}" if s["cost"] > 0 else "$0"
            print(f"  {cmd:<22} {s['requests']:>9,} {s['tokens_in']:>11,} {cost_str:>10}")

    # Costliest individual requests. This is the question the ledger exists
    # to answer: not "what did I spend" but "which requests were expensive,
    # and why" — so the next one can be cheaper on purpose.
    top = costliest(5)
    if top and top[0].get("cost", 0) > 0:
        print("\n  Most Expensive Requests:")
        for e in top:
            if e.get("cost", 0) <= 0:
                continue
            label = f"{e.get('cmd', '?')} -{e.get('mode', '')}".rstrip(" -")
            print(f"  ${e['cost']:>8.4f}  {label:<20} {explain_cost(e)}")

    all_time = stats["all_time"]
    if all_time["cache_hits"] > 0 and all_time["requests"] > 0:
        hit_rate = all_time["cache_hits"] / all_time["requests"] * 100
        print(f"\n  Cache hit rate: {hit_rate:.0f}% ({all_time['cache_hits']} of {all_time['requests']} requests)")

    if all_time["requests"] == 0:
        print("\n  No usage recorded yet. Run a command to start tracking.")
        return

    # Name the basis so the figures are never mistaken for something they
    # aren't. Billed entries come from the CLI's own usage report and ARE the
    # invoice; estimated ones are local token counts and can be far off,
    # because they cannot see the system prompt, CLAUDE.md, tool definitions
    # or prompt-cache traffic that the real request carried.
    billed = all_time["billed_requests"]
    estimated = all_time["estimated_requests"]

    # What the response cache actually saved. A hit costs nothing, so the
    # avoided spend is the mean cost of a real call times the hit count —
    # the clearest argument for keeping the cache.
    hits = all_time["cache_hits"]
    paid = all_time["requests"] - hits
    if hits and paid:
        mean = all_time["cost"] / paid
        print(f"\n  Response cache: {hits:,} hit(s), ~${mean * hits:.4f} avoided")

    cached_in = all_time["cache_read_tokens"]
    if cached_in:
        print(f"  Prompt-cache reads: {cached_in:,} input tokens "
              f"(billed at ~10% of the uncached rate)")

    if billed and not estimated:
        print("\n  Billed figures - reported by the claude CLI itself.")
    elif billed:
        print(f"\n  Mixed basis - {billed:,} request(s) billed by the claude CLI, "
              f"{estimated:,} locally estimated.")
        print("  Estimated entries exclude the system prompt, CLAUDE.md, tool "
              "definitions\n  and cache traffic, so they read low.")
    else:
        from claudio.utils.tokens import PRICING_LAST_UPDATED, counting_method
        method = counting_method()
        print(f"\n  Estimates only - {method} token counts, prices as of "
              f"{PRICING_LAST_UPDATED}.")
        print("  These exclude the system prompt, CLAUDE.md, tool definitions and "
              "cache\n  traffic, so they read low. Newer runs record billed figures.")
        if method != "tiktoken":
            print("  Install `claudio-cli[tokens]` for closer (BPE) counts.")
