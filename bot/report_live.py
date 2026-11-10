"""Live runner report: trades, fills, open positions, realized/unrealized P&L after fees, balance."""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from .live import LIVE_VARIANTS
from .strategy import taker_fee


def _load(p: Path, default):
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _t(ts) -> str:
    return datetime.fromtimestamp(ts).astimezone().strftime("%a %b %d %I:%M %p %Z") if ts else "--"


def render(root: Path, balance: float | None = None, account_positions: dict | None = None,
           account_error: str | None = None) -> str:
    live = Path(root) / "data" / "live"
    hb = _load(live / "heartbeat.json", {})
    marks = _load(live / "engine" / "marks.json", {}).get("marks", {})
    out = ["KALSHI SPORTS BOT - LIVE (real money)"]
    if hb:
        age = int(time.time() - hb.get("at", 0))
        out.append(f"runner heartbeat {age}s ago (cycle {hb.get('cycle')}, pid {hb.get('pid')}), auth "
                   f"{'ok' if hb.get('auth_ok') else 'NOT OK'}, reconcile {'ok' if hb.get('recon_ok') else 'FAILED'}"
                   f"{', KILL FILE PRESENT (no new orders)' if hb.get('kill') else ''}")
        if hb.get("blocked_events"):
            out.append(f"blocked games: {hb['blocked_events']}")
        out.append(f"odds age (s, from paper runner cache): {hb.get('odds_age_s')}")
    else:
        out.append("runner has not written a heartbeat yet")
    if (live / "KILL").exists():
        out.append("KILL file present: data/live/KILL")
    if balance is not None:
        out.append(f"Kalshi balance now: ${balance:.2f} (shared with the BTC bot)")
    if account_error:
        out.append(f"account read failed: {account_error}")
    if account_positions is not None:
        out.append(f"account NFL/NBA positions now: {account_positions or 'none'}")
    tot_r = tot_u = 0.0
    for name in LIVE_VARIANTS:
        st = _load(live / name / "state.json", None)
        out.append("")
        if st is None:
            out.append(f"[{name}] no state yet")
            continue
        closed, opened, intents = st.get("closed", []), st.get("positions", {}), st.get("intents", {})
        realized = sum(c["pnl"] for c in closed)
        unreal = 0.0
        lines = []
        for t, p in opened.items():
            bid = (marks.get(t) or {}).get("bid")
            val = p["contracts"] * bid - taker_fee(p["contracts"], bid) if bid else 0.0
            u = val - p["entry_cost"]
            unreal += u
            lines.append(f"   OPEN {t} {p['contracts']:g}x avg {p['entry_price']:.4f} cost ${p['entry_cost']:.2f} "
                         f"(fees ${p['entry_fee']:.2f}) | bid {bid if bid is not None else '--'} -> "
                         f"unrealized ${u:+.2f} | order {p.get('order_id')} | {_t(p.get('opened_at'))}")
        for c in closed[-10:]:
            lines.append(f"   CLOSED {c['ticker']} {c['contracts']:g}x {c['entry_price']:.4f} -> {c['exit_price']} "
                         f"({c['reason']}) pnl ${c['pnl']:+.2f} | {_t(c.get('closed_at'))}")
        for coid, i in intents.items():
            lines.append(f"   UNRESOLVED ORDER {i['kind']} {i['ticker']} x{i['contracts']} @ {i['limit']} "
                         f"(client id {coid[:8]}, order {i.get('order_id')}) - game blocked until resolved")
        spend = st.get("spend_by_day", {})
        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        out.append(f"[{name}] trades {len(closed) + len(opened)} (open {len(opened)}, closed {len(closed)}) | "
                   f"spent today ${spend.get(today, 0):.2f} | realized ${realized:+.2f} | unrealized ${unreal:+.2f} "
                   f"| total ${realized + unreal:+.2f} (after fees)")
        out += lines
        tot_r += realized
        tot_u += unreal
    out.append("")
    out.append(f"ALL LIVE: realized ${tot_r:+.2f}, unrealized ${tot_u:+.2f}, total ${tot_r + tot_u:+.2f} after fees")
    return "\n".join(out)
