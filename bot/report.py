"""Variant comparison report (reads data/variants/*/state.json + shared marks/closing lines)."""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from .strategy import taker_fee


def _load(p: Path, default):
    return json.loads(p.read_text()) if p.exists() else default


def clv_for(pos: dict, closing: dict, ticker: str) -> tuple[float | None, float | None]:
    """(CLV vs Kalshi last pre-start price, CLV vs sportsbook fair at close) in cents, for pregame entries
    whose game has started. Positive = we bought cheaper than the closing line."""
    c = closing.get(ticker)
    if not c or time.time() < c["start"] or pos.get("opened_at", 0) >= c["start"]:
        return None, None
    close_k = c.get("kalshi_last") if c.get("kalshi_last") is not None else c.get("kalshi_mid")
    k = None if close_k is None else round((close_k - pos["entry_price"]) * 100, 2)
    f = None if c.get("fair") is None else round((c["fair"] - pos["entry_price"]) * 100, 2)
    return k, f


def variant_metrics(state: dict, marks: dict, closing: dict) -> dict:
    closed, opened = state.get("closed", []), state.get("positions", {})
    realized = sum(c["pnl"] for c in closed)
    unreal, unmarked = 0.0, 0
    for t, p in opened.items():
        m = marks.get(t) or {}
        bid = m.get("bid")
        if bid:
            unreal += p["contracts"] * bid - taker_fee(p["contracts"], bid) - p["entry_cost"]
        else:
            unreal -= p["entry_cost"]     # no bid -> worth 0 if dumped now
            unmarked += 1
    capital = sum(c["entry_cost"] for c in closed) + sum(p["entry_cost"] for p in opened.values())
    wins = sum(1 for c in closed if c["pnl"] > 0)
    clv_k, clv_f = [], []
    for rec in list(closed) + [{**p, "ticker": t} for t, p in opened.items()]:
        k, f = clv_for(rec, closing, rec["ticker"])
        if k is not None:
            clv_k.append(k)
        if f is not None:
            clv_f.append(f)
    fees = sum(c.get("entry_fee", 0) + c.get("exit_fee", 0) for c in closed) + \
        sum(p.get("entry_fee", 0) for p in opened.values())
    total = realized + unreal
    return {"trades": len(closed) + len(opened), "closed": len(closed), "wins": wins,
            "win_rate": (wins / len(closed) * 100) if closed else None, "realized": realized,
            "unrealized": unreal, "total": total, "fees": fees, "capital": capital,
            "roi": (total / capital * 100) if capital else None,
            "clv_kalshi": (sum(clv_k) / len(clv_k)) if clv_k else None,
            "clv_fair": (sum(clv_f) / len(clv_f)) if clv_f else None, "clv_n": len(clv_k),
            "open": len(opened), "pending": len(state.get("pending", {})), "unmarked": unmarked}


def _f(x, fmt):
    return "--" if x is None else fmt.format(x)


def render(root: Path, detail: bool = False) -> str:
    vdir, shared = root / "data" / "variants", root / "data" / "shared"
    marks_doc = _load(shared / "marks.json", {"at": 0, "marks": {}})
    closing = _load(shared / "closing_lines.json", {})
    hb = _load(shared / "heartbeat.json", {})
    lines = []
    age = time.time() - hb["at"] if hb else None
    lines.append(f"Report {datetime.now().astimezone():%a %b %d %I:%M %p %Z} | runner heartbeat "
                 f"{'never' if age is None else f'{int(age)}s ago (cycle {hb.get('cycle')})'} | marks "
                 f"{int(time.time() - marks_doc['at'])}s old | Odds API remaining {hb.get('odds_remaining', '?')}")
    hdr = (f"{'variant':<26}{'trades':>7}{'closed':>7}{'win%':>6}{'realized':>10}{'unreal@bid':>11}"
           f"{'total':>9}{'fees':>7}{'capital':>9}{'ROI%':>7}{'CLV k':>7}{'CLV fair':>9}{'n':>3}{'open':>5}{'pend':>5}")
    lines += [hdr, "-" * len(hdr)]
    details = []
    for d in sorted(p for p in vdir.glob("*") if (p / "state.json").exists()):
        st = _load(d / "state.json", {})
        m = variant_metrics(st, marks_doc["marks"], closing)
        lines.append(f"{d.name:<26}{m['trades']:>7}{m['closed']:>7}{_f(m['win_rate'], '{:.0f}'):>6}"
                     f"{m['realized']:>+10.2f}{m['unrealized']:>+11.2f}{m['total']:>+9.2f}{m['fees']:>7.2f}"
                     f"{m['capital']:>9.2f}{_f(m['roi'], '{:+.1f}'):>7}{_f(m['clv_kalshi'], '{:+.1f}'):>7}"
                     f"{_f(m['clv_fair'], '{:+.1f}'):>9}{m['clv_n']:>3}{m['open']:>5}{m['pending']:>5}")
        if detail:
            details.append(f"\n{d.name}: {st.get('variant', {}).get('description', '')}")
            for t, p in st.get("positions", {}).items():
                mk = marks_doc["marks"].get(t, {})
                c = closing.get(t, {})
                details.append(f"  OPEN  {t} {p['contracts']}x @ {p['entry_price']:.2f} cost ${p['entry_cost']:.2f} "
                               f"bid {_f(mk.get('bid'), '{:.2f}')} | close-line k {_f(c.get('kalshi_last'), '{:.2f}')} "
                               f"fair {_f(c.get('fair'), '{:.3f}')} | tags {','.join(p.get('tags', []))}")
            for t, o in st.get("pending", {}).items():
                details.append(f"  BID   {t} {o['contracts']}x @ {o['price']:.2f} expires "
                               f"{datetime.fromtimestamp(o['expires_at']).astimezone():%a %I:%M %p %Z}")
            for c in st.get("closed", [])[-10:]:
                k, f = clv_for(c, closing, c["ticker"])
                details.append(f"  CLOSED {c['ticker']} {c['contracts']}x {c['entry_price']:.2f}->{c['exit_price']:.2f} "
                               f"pnl ${c['pnl']:+.2f} ({c['reason']}) CLV k {_f(k, '{:+.1f}')} | tags "
                               f"{','.join(c.get('tags', []))}")
    lines.append("P&L in $ after Kalshi fees (entry fee + taker exit fee; unrealized = sell now at bid incl. fee). "
                 "ROI = total / capital deployed. CLV in cents vs last pre-start Kalshi price / sportsbook fair "
                 "(pregame entries whose game has started; n = count).")
    return "\n".join(lines + details)
