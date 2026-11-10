#!/usr/bin/env python3
"""Live smoke test against Kalshi's PUBLIC market endpoints (no auth, no orders).
Confirms NFL and NBA game-winner markets are discoverable. Run: python -m tests.check_kalshi_markets"""
import sys

from bot.config import load_config
from bot.kalshi import KalshiClient, group_events


def main():
    cfg = load_config()
    k = KalshiClient(cfg.kalshi_base_url)
    ok = True
    for series in cfg.series:
        mks = k.open_markets(series)
        events = group_events(mks)
        priced = sum(1 for m in mks if m.yes_ask is not None and m.yes_bid is not None)
        print(f"{series}: {len(mks)} open markets across {len(events)} games ({priced} with a bid & ask)")
        for ev, ms in sorted(events.items(), key=lambda kv: kv[1][0].start_time or 0)[:3]:
            st = ms[0].start_time.astimezone().strftime("%a %b %d %I:%M %p %Z") if ms[0].start_time else "?"
            quotes = ", ".join(f"{m.team_abbr} {m.yes_bid}/{m.yes_ask}" for m in ms)
            print(f"   {ev}  {st}  {quotes}")
        ok &= len(mks) > 0
    print("PASS" if ok else "FAIL: a series returned no open markets")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
