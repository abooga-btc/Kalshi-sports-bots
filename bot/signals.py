"""Transparent entry scoring (all values in CENTS of edge-equivalent):

    score = odds_edge + contrarian + news           (per side of a game)

  odds_edge  = (sportsbook no-vig fair prob - Kalshi YES ask) * 100
  contrarian = book + flow + overpay (+ drift), capped at contra_max_cents
               book    : opposing side holds >= lopsided_share of resting bid depth near the top
               flow    : opposing side got >= lopsided_share of taker volume in the last flow_window_hours
               overpay : opposing side's Kalshi mid is above ITS sportsbook fair value (crowd overpaying)
               drift   : ...and it has risen >= drift_min_cents vs Kalshi's previous_price (~24h)
  news       = news_boost_cents * (min(1, neg_opponent) - min(1, neg_self))
               entry BLOCKED if neg_self >= news_block_threshold (fresh negative news on our team)

Gates: volume (game must be heavily traded), price/spread/liquidity filters, odds freshness,
and an odds-edge floor: edge >= edge_cents normally, relaxed to relaxed_edge_cents when
contrarian + news >= strong_signal_cents. Then score >= min_score_cents to enter.
"""
from __future__ import annotations

from dataclasses import dataclass, field


# ---------------- volume ----------------
def game_activity(mks) -> dict:
    return {"volume": sum(m.volume for m in mks), "volume_24h": sum(m.volume_24h for m in mks),
            "open_interest": sum(m.open_interest for m in mks)}


def volume_gate(events: dict, cfg) -> dict[str, dict]:
    """event_ticker -> {volume, volume_24h, open_interest, rank_pct, pass, why}. Rank is within sport."""
    by_sport: dict[str, list] = {}
    for ev, mks in events.items():
        by_sport.setdefault(mks[0].sport, []).append((ev, game_activity(mks)))
    out = {}
    for sport, rows in by_sport.items():
        rows.sort(key=lambda r: -r[1]["volume"])
        n = len(rows)
        for i, (ev, act) in enumerate(rows):
            top_pct = i / n * 100                  # share of games ranked above this one (1st -> 0%)
            ok_floor = act["volume"] >= cfg.min_game_volume and act["volume_24h"] >= cfg.min_game_volume_24h
            ok_rank = top_pct < cfg.volume_top_pct
            why = []
            if not ok_floor:
                why.append(f"vol {act['volume']:,.0f}/24h {act['volume_24h']:,.0f} < floor "
                           f"{cfg.min_game_volume:,.0f}/{cfg.min_game_volume_24h:,.0f}")
            if not ok_rank:
                why.append(f"rank {i + 1}/{n} not in top {cfg.volume_top_pct:.0f}%")
            out[ev] = {**act, "rank_pct": round(top_pct, 1), "rank": f"{i + 1}/{n}", "pass": ok_floor and ok_rank, "why": "; ".join(why) or "ok"}
    return out


# ---------------- demand / contrarian ----------------
def depth_near_top(levels: list, window: float) -> float:
    """levels: ascending [(price, size)] of resting bids. Sum size within `window` dollars of the best bid."""
    if not levels:
        return 0.0
    best = levels[-1][0]
    return sum(q for p, q in levels if p >= best - window - 1e-9)


@dataclass
class Demand:
    """Demand for each TEAM in a game (keys = Kalshi team abbr)."""
    book: dict = field(default_factory=dict)   # resting bid depth near top
    flow: dict = field(default_factory=dict)   # taker contracts in window

    @staticmethod
    def share(d: dict, team: str) -> float | None:
        tot = sum(d.values())
        return None if tot <= 0 else d.get(team, 0.0) / tot


def game_demand(markets, books: dict, trades: dict, window: float) -> Demand:
    """Long-team-X demand = YES bids/takers on X's market + NO bids/takers on the other team's market."""
    dm = Demand()
    abbrs = [m.team_abbr for m in markets]
    for m in markets:
        others = [a for a in abbrs if a != m.team_abbr]
        ob = books.get(m.ticker) or {}
        dm.book[m.team_abbr] = dm.book.get(m.team_abbr, 0) + depth_near_top(ob.get("yes", []), window)
        for o in others:
            dm.book[o] = dm.book.get(o, 0) + depth_near_top(ob.get("no", []), window)
        for t in trades.get(m.ticker) or []:
            if t["side"] == "yes":
                dm.flow[m.team_abbr] = dm.flow.get(m.team_abbr, 0) + t["count"]
            elif t["side"] == "no":
                for o in others:
                    dm.flow[o] = dm.flow.get(o, 0) + t["count"]
    return dm


def _lopsided(share_opp: float | None, cfg, max_cents: float) -> float:
    if share_opp is None or share_opp <= cfg.lopsided_share:
        return 0.0
    return round((share_opp - cfg.lopsided_share) / (1 - cfg.lopsided_share) * max_cents, 2)


def contrarian_score(team: str, opp_mk, opp_fair: float, demand: Demand, cfg) -> dict:
    opp = opp_mk.team_abbr
    flow_total = sum(demand.flow.values())
    book_share = Demand.share(demand.book, opp)
    flow_share = Demand.share(demand.flow, opp) if flow_total >= cfg.min_flow_contracts else None
    book_c = _lopsided(book_share, cfg, cfg.book_max_cents)
    flow_c = _lopsided(flow_share, cfg, cfg.flow_max_cents)
    over = None
    over_c = drift_c = 0.0
    drift = None
    if opp_mk.mid is not None:
        over = (opp_mk.mid - opp_fair) * 100
        over_c = round(min(cfg.overpay_max_cents, max(0.0, over - cfg.overpay_min_cents)), 2)
        if opp_mk.previous_price and opp_mk.last_price:
            drift = (opp_mk.last_price - opp_mk.previous_price) * 100
            if over_c > 0 and drift >= cfg.drift_min_cents:
                drift_c = cfg.drift_bonus_cents
    total = round(min(cfg.contra_max_cents, book_c + flow_c + over_c + drift_c), 2)
    return {"total": total, "book_c": book_c, "flow_c": flow_c, "overpay_c": over_c, "drift_c": drift_c,
            "opp_book_share": None if book_share is None else round(book_share, 3),
            "opp_flow_share": None if flow_share is None else round(flow_share, 3),
            "flow_contracts": round(flow_total), "opp_overpay_cents": None if over is None else round(over, 1),
            "opp_drift_cents": None if drift is None else round(drift, 1)}


# ---------------- news ----------------
def news_score(team: str, opp: str, team_news: dict, cfg) -> dict:
    neg_self = team_news[team].neg if team in team_news else 0.0
    neg_opp = team_news[opp].neg if opp in team_news else 0.0
    blocked = cfg.news_block_enabled and neg_self >= cfg.news_block_threshold
    boost = min(1.0, neg_opp) if cfg.news_boost_enabled else 0.0
    total = round(cfg.news_boost_cents * (boost - min(1.0, neg_self)), 2)
    return {"total": total, "neg_self": neg_self, "neg_opp": neg_opp, "blocked": blocked}


# ---------------- combine ----------------
def combine(edge_cents: float, contra: dict, news: dict, cfg) -> tuple[bool, float, str]:
    """Return (enter?, score, reason)."""
    signal = contra["total"] + news["total"]
    score = round(edge_cents + signal, 2)
    if news["blocked"]:
        return False, score, f"BLOCKED by news (neg {news['neg_self']:.2f} >= {cfg.news_block_threshold})"
    strong = signal >= cfg.strong_signal_cents
    floor = cfg.relaxed_edge_cents if strong else cfg.edge_cents
    if edge_cents + 1e-9 < floor:
        return False, score, (f"edge {edge_cents:+.1f}c < floor {floor}c"
                              + (" (relaxed: strong signals)" if strong else f" (signals {signal:+.1f}c < strong {cfg.strong_signal_cents}c)"))
    if score + 1e-9 < cfg.min_score_cents:
        return False, score, f"score {score:+.1f}c < {cfg.min_score_cents}c"
    return True, score, f"score {score:+.1f}c >= {cfg.min_score_cents}c" + (" (relaxed edge floor)" if strong and edge_cents < cfg.edge_cents else "")
