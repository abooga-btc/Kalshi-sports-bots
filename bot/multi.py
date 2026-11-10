"""Multi-variant paper trading.

ONE process fetches market data once per cycle (Kalshi markets/orderbooks/trades, Odds API h2h odds,
ESPN news) and feeds it to several independent simulated portfolios ("variants"), each with its own
state / trade log / P&L under data/variants/<name>/. Dry-run only: this module never sends orders.

Per side of a game the shared analysis computes (see bot/signals.py):
    raw_edge  = (sportsbook no-vig fair - entry price) * 100           [cents]
    net_edge  = raw_edge - entry_fee/contract - exit_fee/contract(at fair) - spread/2     (fee-aware variants)
    contrarian, news                                                    [cents]
Each variant then applies its own rule (VariantSpec) to the same numbers.
"""
from __future__ import annotations

import json
import pickle
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from .kalshi import KalshiError
from .ledger import Ledger
from .main import SETTLED_STATUSES, Bot, log
from .signals import Demand, combine, contrarian_score, news_score, volume_gate
from .strategy import exit_signal, maker_fee, match_game, taker_fee


# ---------------------------------------------------------------- variants
@dataclass(frozen=True)
class VariantSpec:
    name: str
    description: str
    entry: str = "combined"         # "combined" = edge + contrarian + news score; "odds_only" = edge floor only
    edge_floor_cents: float = 5.0   # edge floor (raw edge, or net edge when fee_aware)
    fee_aware: bool = False         # use net_edge (after entry+exit fees and half the spread) instead of raw edge
    use_contrarian: bool = True
    use_news_boost: bool = True     # the news BLOCK is always on for every variant
    maker: bool = False             # simulate a resting bid 1c above the best bid instead of taking the ask
    take_profit: bool = True        # False = hold to settlement


VARIANTS: list[VariantSpec] = [
    VariantSpec("A_current", "combined score (edge+contrarian+news), raw edge floor 5c (0c if signals >=3c), "
                "score>=5c, taker at ask", "combined", 5.0),
    VariantSpec("B_odds_only_5c", "raw edge >=5c at the ask, news block, volume gate", "odds_only", 5.0,
                use_contrarian=False, use_news_boost=False),
    VariantSpec("C_odds_only_3c_fee_aware", "net edge (after fees+half spread) >=3c, news block, volume gate",
                "odds_only", 3.0, fee_aware=True, use_contrarian=False, use_news_boost=False),
    VariantSpec("D_no_contrarian", "net edge >=3c and net edge + news >=5c (no contrarian), taker",
                "combined", 3.0, fee_aware=True, use_contrarian=False),
    VariantSpec("E_upgraded", "combined score on net edge at a maker price (bid+1c), net floor 3c (0c if signals "
                ">=3c), score>=5c, maker entry", "combined", 3.0, fee_aware=True, maker=True),
    VariantSpec("F_hold_to_settle", "same entries as C, no take-profit: hold to settlement",
                "odds_only", 3.0, fee_aware=True, use_contrarian=False, use_news_boost=False, take_profit=False),
]


def size_for_budget(budget: float, price: float, fee_fn) -> int:
    if price <= 0:
        return 0
    n = int(budget // price)
    while n > 0 and n * price + fee_fn(n, price) > budget + 1e-9:
        n -= 1
    return n


def evaluate_side(spec: VariantSpec, cfg, side: dict) -> dict:
    """Apply one variant's rule to one side of a game. Returns a decision dict (ok, reason, sizing, tags)."""
    mk, fair = side["mk"], side["fair"]
    out = {"ok": False, "ticker": mk.ticker, "abbr": mk.team_abbr}
    if mk.status not in ("active", "open"):
        return {**out, "reason": f"status={mk.status}"}
    if spec.maker:
        if mk.yes_bid is None or mk.yes_ask is None:
            return {**out, "reason": "no two-sided quote for a maker bid"}
        price = round(mk.yes_bid + cfg.maker_offset_cents / 100.0, 4)
        if price >= mk.yes_ask - 1e-9:          # 1c spread: join the best bid rather than cross
            price = mk.yes_bid
        fee_fn = lambda n, p: maker_fee(n, p, cfg.maker_fee_rate)
    else:
        price = mk.yes_ask
        fee_fn = lambda n, p: taker_fee(n, p, cfg.taker_fee_rate)
    if price is None or price <= 0:
        return {**out, "reason": "no price"}
    if not (cfg.min_price <= price <= cfg.max_price):
        return {**out, "reason": f"price {price:.2f} outside [{cfg.min_price},{cfg.max_price}]"}
    if mk.spread is not None and mk.spread > cfg.max_spread + 1e-9:
        return {**out, "reason": f"spread {mk.spread:.2f} > {cfg.max_spread}"}
    n = size_for_budget(cfg.trade_size_usd, price, fee_fn)
    if not spec.maker and mk.yes_ask_size is not None:
        n = min(n, int(mk.yes_ask_size))
    if n <= 0:
        return {**out, "reason": "size rounds to 0 contracts"}
    fee = fee_fn(n, price)
    raw = (fair - price) * 100
    entry_fee_c = fee / n * 100
    exit_fee_c = taker_fee(n, fair, cfg.taker_fee_rate) / n * 100
    half_spread_c = (mk.spread or 0.0) * 50
    net = raw - entry_fee_c - exit_fee_c - half_spread_c
    edge = net if spec.fee_aware else raw
    contra_t = side["contra"]["total"] if spec.use_contrarian else 0.0
    news = dict(side["news"])
    if not spec.use_news_boost:
        news["total"] = 0.0
    if spec.entry == "odds_only":
        score = round(edge, 2)
        if news["blocked"]:
            ok, why = False, f"BLOCKED by news (neg {news['neg_self']:.2f})"
        elif edge + 1e-9 < spec.edge_floor_cents:
            ok, why = False, f"{'net' if spec.fee_aware else 'raw'} edge {edge:+.1f}c < {spec.edge_floor_cents}c"
        else:
            ok, why = True, f"{'net' if spec.fee_aware else 'raw'} edge {edge:+.1f}c >= {spec.edge_floor_cents}c"
    else:
        vcfg = replace(cfg, edge_cents=spec.edge_floor_cents)
        ok, score, why = combine(edge, {"total": contra_t}, news, vcfg)
    tags = [f"{'net_' if spec.fee_aware else ''}edge{edge:+.1f}"]
    if spec.use_contrarian:
        tags += [k[:-2] for k in ("book_c", "flow_c", "overpay_c", "drift_c") if side["contra"].get(k, 0) > 0]
    if spec.use_news_boost and news["total"] > 0:
        tags.append("news_boost")
    if spec.use_news_boost and news["total"] < 0:
        tags.append("news_penalty")
    if "relaxed" in why:
        tags.append("relaxed_floor")
    tags.append("maker" if spec.maker else "taker")
    if spec.fee_aware:
        tags.append("fee_aware")
    return {**out, "ok": ok, "reason": why, "score": score, "price": price, "contracts": n, "fee": round(fee, 4),
            "cost": round(n * price + fee, 4), "raw_edge_c": round(raw, 2), "net_edge_c": round(net, 2),
            "contra_c": round(contra_t, 2), "news_c": news["total"], "tags": tags,
            "team": side["team"], "fair": fair, "event_ticker": mk.event_ticker}


# ---------------------------------------------------------------- portfolio
class Portfolio:
    def __init__(self, spec: VariantSpec, cfg, root: Path):
        self.spec = spec
        self.cfg = replace(cfg, include_fees=True,
                           take_profit_pct=cfg.take_profit_pct if spec.take_profit else 1e9)
        d = Path(root) / spec.name
        self.ledger = Ledger(d / "state.json", d / "trades.csv", d / "events.jsonl", "DRY_RUN")
        self.ledger.state.setdefault("pending", {})
        self.ledger.state["variant"] = {"name": spec.name, "description": spec.description, **spec.__dict__}
        self.ledger.save()

    @property
    def pending(self) -> dict:
        return self.ledger.state["pending"]

    def log(self, msg):
        log(f"[{self.spec.name}] {msg}")

    # ----- exits / settlement -----
    def manage_exits(self, get_market):
        L = self.ledger
        for ticker, pos in list(L.positions.items()):
            mk = get_market(ticker)
            if mk is None:
                continue
            if mk.status in SETTLED_STATUSES and mk.result in ("yes", "no"):
                payout = float(pos["contracts"]) if mk.result == "yes" else 0.0
                pnl = L.close_position(ticker, 1.0 if mk.result == "yes" else 0.0, 0.0, payout, f"settled_{mk.result}")
                self.log(f"SETTLED {ticker} result={mk.result} pnl=${pnl:+.2f}")
                continue
            if not self.spec.take_profit:
                continue
            do_exit, reason, value = exit_signal(pos["entry_cost"], pos["contracts"], mk.yes_bid, self.cfg)
            if do_exit:
                fee = round(pos["contracts"] * mk.yes_bid - value, 4)
                pnl = L.close_position(ticker, mk.yes_bid, fee, round(value, 4), reason)
                self.log(f"WOULD SELL {ticker} {pos['contracts']} @ {mk.yes_bid:.2f} ({reason}) pnl=${pnl:+.2f}")

    # ----- simulated maker orders -----
    def process_pending(self, get_market, trades_since, started, now_ts=None):
        now_ts = now_ts or time.time()
        L, cfg = self.ledger, self.cfg
        for ticker, o in list(self.pending.items()):
            mk = get_market(ticker)
            trades = trades_since(ticker, o["placed_at"])
            hit = sum(t["count"] for t in trades
                      if t.get("side") == "no" and t.get("yes_price", 1) <= o["price"] + 1e-9
                      and _ts(t.get("ts")) > o["placed_at"])
            filled = min(o["contracts"], int(hit))
            how = f"{int(hit)} contracts sold at <= {o['price']:.2f}"
            if mk is not None and mk.yes_ask is not None and mk.yes_ask <= o["price"] + 1e-9:
                filled, how = o["contracts"], f"ask reached {mk.yes_ask:.2f}"
            done = filled >= o["contracts"]
            cancel_why = None
            if not done:
                if now_ts >= o["expires_at"]:
                    cancel_why = "timeout"
                elif cfg.maker_cancel_at_start and started(ticker) and not o.get("placed_live"):
                    cancel_why = "game started"
                elif mk is not None and mk.status not in ("active", "open"):
                    cancel_why = f"market {mk.status}"
            if not done and cancel_why is None:
                continue
            del self.pending[ticker]
            if filled > 0:
                fee = maker_fee(filled, o["price"], cfg.maker_fee_rate)
                cost = round(filled * o["price"] + fee, 4)
                L.add_spend(cost - o["cost"], o["day"])          # refund the unfilled remainder
                L.open_position(ticker, o["team"], filled, o["price"], fee, cost, o["fair"], o["edge"],
                                o["event_ticker"], count_spend=False, reason="|".join(o["tags"]),
                                tags=o["tags"], score=o["score"], entry_kind="maker",
                                placed_at=o["placed_at"], fill_note=how)
                self.log(f"MAKER FILL {filled}/{o['contracts']}x {ticker} @ {o['price']:.2f} ({how}"
                         f"{', partial at ' + cancel_why if not done else ''}) cost=${cost:.2f}")
                L.event("maker_fill", ticker=ticker, filled=filled, of=o["contracts"], price=o["price"], how=how)
            else:
                L.add_spend(-o["cost"], o["day"])
                L.save()
                self.log(f"MAKER CANCEL {ticker} bid {o['price']:.2f} ({cancel_why}, no fill)")
                L.event("maker_cancel", ticker=ticker, price=o["price"], why=cancel_why)

    # ----- entries -----
    def consider(self, rows) -> list[dict]:
        decisions = []
        L, cfg = self.ledger, self.cfg
        held_events = {p["event_ticker"] for p in L.positions.values()} | \
                      {o["event_ticker"] for o in self.pending.values()}
        for row in rows:
            evals = [evaluate_side(self.spec, cfg, s) for s in row["sides"]]
            for e in evals:
                if row["game_block"]:
                    e["ok"], e["reason"] = False, row["game_block"]
            cands = [e for e in evals if e["ok"]]
            for e in evals:
                decisions.append({"game": row["game"], **{k: e.get(k) for k in
                                  ("abbr", "ok", "reason", "score", "raw_edge_c", "net_edge_c", "contra_c", "news_c")}})
            if not cands:
                continue
            best = max(cands, key=lambda e: e["score"])   # never both sides of a game
            msg = self._try_enter(best, row, held_events)
            decisions[-len(evals) + evals.index(best)]["reason"] += f" -> {msg}"
        return decisions

    def _try_enter(self, e, row, held_events) -> str:
        L, cfg = self.ledger, self.cfg
        if e["event_ticker"] in held_events:
            return "skip: already holding/bidding this game"
        cool = cfg.reentry_cooldown_min * 60
        if any(c.get("event_ticker") == e["event_ticker"] and time.time() - c.get("closed_at", 0) < cool
               for c in L.state["closed"]):
            return f"skip: exited this game < {cfg.reentry_cooldown_min} min ago"
        if len(L.positions) + len(self.pending) >= cfg.max_open_positions:
            return f"skip: max_open_positions={cfg.max_open_positions}"
        if L.spent_today() + e["cost"] > cfg.max_daily_spend_usd + 1e-9:
            return f"skip: daily spend ${L.spent_today():.2f}+${e['cost']:.2f} > ${cfg.max_daily_spend_usd}"
        held_events.add(e["event_ticker"])
        common = dict(fair=e["fair"], edge=round((e["fair"] - e["price"]), 4), event_ticker=e["event_ticker"],
                      tags=e["tags"], score=e["score"])
        if self.spec.maker:
            now = time.time()
            self.pending[e["ticker"]] = {"team": e["team"], "price": e["price"], "contracts": e["contracts"],
                                         "cost": e["cost"], "placed_at": now, "day": L.today(), "placed_live": row["live"],
                                         "expires_at": now + cfg.maker_timeout_min * 60, **common}
            L.add_spend(e["cost"])
            L.save()
            L.event("maker_bid", ticker=e["ticker"], **{k: e[k] for k in ("price", "contracts", "cost", "score", "tags")})
            self.log(f"MAKER BID {e['contracts']}x {e['ticker']} @ {e['price']:.2f} (score {e['score']:+.1f}c, "
                     f"tags {','.join(e['tags'])})")
            return f"MAKER BID {e['contracts']} @ {e['price']:.2f}"
        L.open_position(e["ticker"], e["team"], e["contracts"], e["price"], e["fee"], e["cost"], common["fair"],
                        common["edge"], e["event_ticker"], reason="|".join(e["tags"]), tags=e["tags"],
                        score=e["score"], entry_kind="taker", raw_edge_c=e["raw_edge_c"], net_edge_c=e["net_edge_c"])
        L.event("entry", ticker=e["ticker"], **{k: e[k] for k in ("team", "contracts", "price", "fee", "cost",
                                                                  "score", "raw_edge_c", "net_edge_c", "tags")})
        self.log(f"WOULD BUY {e['contracts']}x {e['ticker']} @ {e['price']:.2f} fair={e['fair']:.3f} "
                 f"score {e['score']:+.1f}c cost=${e['cost']:.2f} tags {','.join(e['tags'])}")
        return f"WOULD BUY {e['contracts']} @ {e['price']:.2f} (cost ${e['cost']:.2f})"


def _ts(s) -> float:
    if not s:
        return 0.0
    if isinstance(s, (int, float)):
        return float(s)
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()


# ---------------------------------------------------------------- engine
class Engine(Bot):
    """Shared data pipeline (inherits Kalshi/odds/ESPN plumbing from Bot) + N portfolios."""

    def __init__(self, cfg, specs=None):
        shared = cfg.path(cfg.shared_dir)
        scfg = replace(cfg, state_file=str(shared / "state.json"), trade_log_csv=str(shared / "unused.csv"),
                       event_log_jsonl=str(shared / "events.jsonl"))
        super().__init__(scfg, live=False)
        self.base_cfg = cfg
        self.shared = shared
        self.portfolios = [Portfolio(s, cfg, cfg.path(cfg.variants_dir)) for s in (specs or VARIANTS)]
        self.closing_path = shared / "closing_lines.json"
        self.closing = json.loads(self.closing_path.read_text()) if self.closing_path.exists() else {}
        self.odds_cache_path = shared / "odds_cache.pkl"
        if self.odds_cache_path.exists():
            try:
                self.odds_cache = pickle.loads(self.odds_cache_path.read_bytes())
            except Exception as e:   # corrupt cache is not fatal
                log(f"odds cache unreadable ({type(e).__name__}); starting empty")
        self.cycle = 0

    # ----- shared data -----
    def refresh_odds(self, need, force=False):
        before = {s: v[0] for s, v in self.odds_cache.items()}
        missing = {s: v for s, v in need.items() if s not in self.odds_cache}
        if missing and not force:     # bootstrap: a sport we have never priced (e.g. first start)
            super().refresh_odds(missing, force=True)
            need = {s: v for s, v in need.items() if s not in missing}
        super().refresh_odds(need, force=force)
        if {s: v[0] for s, v in self.odds_cache.items()} != before:
            tmp = self.odds_cache_path.with_suffix(".tmp")
            tmp.write_bytes(pickle.dumps(self.odds_cache))
            tmp.replace(self.odds_cache_path)

    def analyze(self, events, now) -> list[dict]:
        cfg = self.cfg
        rows = []
        vol = volume_gate(events, cfg)
        for sport, (fetched_at, games) in self.odds_cache.items():
            team_news = self.news.team_news(sport) if (cfg.news_enabled and self.news) else {}
            for g in games:
                pairs = match_game(g, events)
                if len(pairs) != 2:
                    continue
                ev = pairs[0][0].event_ticker
                mks = events[ev]
                live = g.is_live(now)
                age = time.time() - fetched_at
                max_age = cfg.max_odds_age_live_sec if live else cfg.max_odds_age_pregame_sec
                v = vol.get(ev, {"pass": False, "why": "no volume data", "volume": 0, "volume_24h": 0,
                                 "open_interest": 0, "rank_pct": 100})
                block = None
                if not v["pass"]:
                    block = f"volume gate: {v['why']}"
                elif g.n_books < cfg.min_books:
                    block = f"only {g.n_books} books"
                elif age > max_age:
                    block = f"odds stale ({int(age)}s > {max_age}s)"
                elif live and not cfg.allow_live_entries:
                    block = "live entries disabled"
                demand = self.microstructure(mks) if (v["pass"] and cfg.contrarian_enabled) else Demand()
                fair_by = {mk.team_abbr: g.fair[team] for mk, team in pairs}
                sides = []
                for mk, team in pairs:
                    opp = next(m for m, _ in pairs if m.ticker != mk.ticker)
                    contra = contrarian_score(mk.team_abbr, opp, fair_by[opp.team_abbr], demand, cfg)
                    if not v["pass"]:
                        contra["book_c"] = contra["flow_c"] = 0.0
                    news = news_score(mk.team_abbr, opp.team_abbr, team_news, cfg) if cfg.news_enabled else \
                        {"total": 0.0, "neg_self": 0.0, "neg_opp": 0.0, "blocked": False}
                    sides.append({"mk": mk, "team": team, "fair": g.fair[team], "contra": contra, "news": news,
                                  "edge_c": round((g.fair[team] - mk.yes_ask) * 100, 2) if mk.yes_ask else None})
                rows.append({"sport": sport, "event": ev, "game": f"{g.away} @ {g.home}", "start": g.commence,
                             "live": live, "books": g.n_books, "odds_age_s": int(age), "volume": v,
                             "game_block": block, "sides": sides,
                             "demand": {"book": {k: round(x) for k, x in demand.book.items()},
                                        "flow": {k: round(x) for k, x in demand.flow.items()}}})
        return rows

    def update_closing(self, rows, now):
        """Record each market's last pre-start Kalshi price and sportsbook fair (frozen once the game starts)."""
        changed = False
        for r in rows:
            if now >= r["start"]:
                continue
            for s in r["sides"]:
                mk = s["mk"]
                self.closing[mk.ticker] = {"start": r["start"].timestamp(), "kalshi_last": mk.last_price,
                                           "kalshi_mid": mk.mid, "kalshi_bid": mk.yes_bid, "kalshi_ask": mk.yes_ask,
                                           "fair": round(s["fair"], 4), "odds_age_s": r["odds_age_s"],
                                           "at": now.timestamp()}
                changed = True
        if changed:
            _write_json(self.closing_path, self.closing)

    def trades_since(self, ticker, since_ts):
        key = (ticker, int(since_ts))
        if key not in self._trade_cache:
            try:
                self._trade_cache[key] = self.kalshi.trades(ticker, int(since_ts))
            except KalshiError as e:
                log(f"trades fetch failed {ticker}: {e}")
                self._trade_cache[key] = []
        return self._trade_cache[key]

    # ----- cycle -----
    def step(self, force_odds: bool = False):
        self.cycle += 1
        self._trade_cache = {}
        now = datetime.now(timezone.utc)
        markets, events = self.kalshi_snapshot()
        by_ticker = {m.ticker: m for m in markets}
        need = self.sports_needing_odds(events, now)
        log(f"cycle {self.cycle}: Kalshi {len(markets)} open markets / {len(events)} games; odds needed: "
            f"{ {s: ('LIVE' if v else 'pregame') for s, v in need.items()} }")
        self.refresh_odds(need, force=force_odds)
        rows = self.analyze(events, now)
        self.update_closing(rows, now)
        self.event("cycle", games=len(rows), rows=[{"event": r["event"], "game": r["game"], "live": r["live"],
                   "volume": r["volume"], "block": r["game_block"], "demand": r["demand"],
                   "sides": [{"ticker": s["mk"].ticker, "fair": round(s["fair"], 4), "bid": s["mk"].yes_bid,
                              "ask": s["mk"].yes_ask, "edge_c": s["edge_c"], "contra": s["contra"],
                              "news": s["news"]} for s in r["sides"]]} for r in rows])

        extra: dict = {}

        def get_market(t):
            if t in by_ticker:
                return by_ticker[t]
            if t not in extra:
                try:
                    extra[t] = self.kalshi.market(t)
                except KalshiError as e:
                    log(f"could not refresh {t}: {e}")
                    extra[t] = None
            return extra[t]

        def started(t):
            c = self.closing.get(t)
            st = c["start"] if c else (get_market(t).start_time.timestamp()
                                       if get_market(t) and get_market(t).start_time else None)
            return st is not None and time.time() >= st

        all_decisions = {}
        for p in self.portfolios:
            try:
                p.process_pending(get_market, self.trades_since, started)
                p.manage_exits(get_market)
                all_decisions[p.spec.name] = p.consider(rows)
            except Exception as e:   # one variant's bug must not kill the others
                p.log(f"variant error: {type(e).__name__}: {e}")
        self.write_marks(by_ticker, extra)
        self.heartbeat()
        for p in self.portfolios:
            L = p.ledger
            p.log(f"open {len(L.positions)} pending {len(p.pending)} | spent today ${L.spent_today():.2f} | "
                  f"realized ${L.state['realized_pnl']:+.2f}")
        return {"rows": rows, "decisions": all_decisions}, any(need.values())

    def event(self, kind, **data):
        self.ledger.event(kind, **data)

    def write_marks(self, by_ticker, extra):
        marks = {t: {"bid": m.yes_bid, "ask": m.yes_ask, "last": m.last_price, "status": m.status,
                     "result": m.result} for t, m in list(by_ticker.items()) + [(t, m) for t, m in extra.items() if m]}
        _write_json(self.shared / "marks.json", {"at": time.time(), "marks": marks})

    def heartbeat(self):
        _write_json(self.shared / "heartbeat.json", {"at": time.time(),
                    "at_local": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "cycle": self.cycle, "odds_remaining": self.odds.remaining,
                    "variants": [p.spec.name for p in self.portfolios]})


def _write_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str))
    tmp.replace(path)


# ---------------------------------------------------------------- migration
def migrate_legacy(cfg, variant: str = "A_current") -> str:
    """Move the single-strategy data/state.json (positions, spend, P&L) into variant A, and its odds
    bookkeeping into the shared state. Idempotent: does nothing once data/legacy/ exists."""
    data = cfg.path("data")
    legacy_dir = data / "legacy"
    old = data / "state.json"
    if legacy_dir.exists() or not old.exists():
        return "nothing to migrate"
    st = json.loads(old.read_text())
    vdir = cfg.path(cfg.variants_dir) / variant
    vdir.mkdir(parents=True, exist_ok=True)
    vstate = {"positions": st.get("positions", {}), "spend_by_day": st.get("spend_by_day", {}),
              "realized_pnl": st.get("realized_pnl", 0.0), "closed": st.get("closed", []), "pending": {}}
    for pos in vstate["positions"].values():
        pos.setdefault("tags", ["migrated_from_single_strategy"])
        pos.setdefault("entry_kind", "taker")
    _write_json(vdir / "state.json", vstate)
    shared = cfg.path(cfg.shared_dir)
    _write_json(shared / "state.json", {"positions": {}, "spend_by_day": {}, "realized_pnl": 0.0, "closed": [],
                                        "odds_last_fetch": st.get("odds_last_fetch", {}),
                                        "odds_remaining": st.get("odds_remaining")})
    if (data / "trades.csv").exists():
        (vdir / "trades.csv").write_bytes((data / "trades.csv").read_bytes())
    legacy_dir.mkdir()
    for f in ("state.json", "trades.csv", "events.jsonl"):
        if (data / f).exists():
            (data / f).rename(legacy_dir / f)
    return f"migrated {len(vstate['positions'])} open position(s) into {variant}"


def main(argv=None):
    import argparse
    import sys
    from .config import ROOT, load_config
    from .envfile import load_dotenv
    from .odds import MissingOddsKey

    ap = argparse.ArgumentParser(description="Multi-variant DRY-RUN simulator (never places orders)")
    ap.add_argument("--config", default=None)
    ap.add_argument("--once", action="store_true", help="run one cycle and exit (odds pacing still applies)")
    ap.add_argument("--force-odds", action="store_true", help="with --once: fetch odds now regardless of pacing")
    args = ap.parse_args(argv)
    load_dotenv(ROOT / ".env")
    cfg = load_config(args.config)
    log(migrate_legacy(cfg))
    try:
        eng = Engine(cfg)
    except MissingOddsKey as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    log(f"MULTI-VARIANT DRY RUN (no orders are ever sent). Variants: {', '.join(p.spec.name for p in eng.portfolios)}")
    try:
        if args.once:
            eng.step(force_odds=args.force_odds)
        else:
            eng.run()
    except MissingOddsKey as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        log("stopped.")
    return 0
