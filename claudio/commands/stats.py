"""claudio stats -- view token usage and cost tracking.

Usage:
    claudio stats               Show usage summary
    claudio stats --reset       Clear all usage data
    claudio stats --json        Output as JSON
"""

import json

from claudio.usage import get_stats, reset_stats
from claudio.cache import cache_clear


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

    all_time = stats["all_time"]
    if all_time["cache_hits"] > 0 and all_time["requests"] > 0:
        hit_rate = all_time["cache_hits"] / all_time["requests"] * 100
        print(f"\n  Cache hit rate: {hit_rate:.0f}% ({all_time['cache_hits']} of {all_time['requests']} requests)")

    if all_time["requests"] == 0:
        print("\n  No usage recorded yet. Run a command to start tracking.")
        return

    # Accuracy caveat: costs are estimates from local token counts, not billed
    # amounts. Name the basis so the figures aren't mistaken for an invoice.
    from claudio.utils.tokens import counting_method, PRICING_LAST_UPDATED
    basis = counting_method()
    print(f"\n  Estimates only - {basis} token counts, prices as of {PRICING_LAST_UPDATED}.")
    if basis != "tiktoken":
        print("  Install `claudio-cli[tokens]` for closer (BPE) counts.")
