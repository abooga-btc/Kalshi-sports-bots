"""LIVE (real-money) runner for the approved variants: F_hold_to_settle and D_no_contrarian.

Same data pipeline and entry rules as the paper simulator (bot/multi.py: Engine.analyze + evaluate_side), but
entries/exits are real Kalshi IOC limit orders and positions are booked from ACTUAL fills only.

Safety design
- Explicit: run_live.py refuses to start without --live and prints a banner.
- Odds: never calls The Odds API. It reads the paper runner's odds cache (data/shared/odds_cache.pkl), so the
  shared monthly quota is not double-consumed. If the paper runner stops, odds go stale and the normal
  staleness gate blocks entries (fail-closed).
- Orders: buy = IOC limit at the ask the strategy evaluated (never higher), size so cost incl. fee <= $10.
  D sells with a reduce-only IOC at the bid when the 15% take-profit triggers; F never sells (holds to settle).
- Idempotency: an "intent" (client_order_id) is saved BEFORE every POST. A POST that errors/times out is never
  re-sent; the intent is resolved later by looking the order up by client_order_id and reading its fills. While
  an intent is unresolved, that game is blocked and the intent counts toward max open positions.
- Reconciliation every cycle (and at startup) against GET /portfolio/positions (NFL/NBA game tickers only).
  Untracked exchange contracts on a game block new buys on that game. If the positions call fails, no orders
  are placed that cycle.
- Global guards: data/live/KILL halts all new orders; no new buy if the balance would drop below
  live_balance_floor_usd ($50 default, protects the BTC bot on the same account); any auth/order error is
  logged and skipped.
"""
from __future__ import annotations

import json
import os
import pickle
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from .kalshi import KalshiClient, KalshiError, load_private_key_from_env
from .ledger import Ledger
from .main import SETTLED_STATUSES, log
from .multi import VARIANTS, Engine, Portfolio, _write_json
from .strategy import exit_signal

LIVE_VARIANTS = ("F_hold_to_settle", "D_no_contrarian")
LIVE_EDGE_FLOOR_CENTS = 0.0
SPORTS_PREFIXES = ("KXNFLGAME-", "KXNBAGAME-")
INTENT_LOOKUP_GRACE_SEC = 90       # an intent whose order cannot be found after this long never reached Kalshi
FILL_POLL_TRIES = 4


class Guard:
    """Global safety checks shared by all live variants."""

    def __init__(self, client, kill_path: Path, balance_floor: float):
        self.client, self.kill_path, self.floor = client, Path(kill_path), float(balance_floor)
        self.recon_ok = False             # positions reconciled successfully this cycle
        self.blocked_events: dict[str, str] = {}   # event_ticker -> reason (untracked exchange position etc.)
        self.auth_ok: bool | None = None
        self.last_balance: float | None = None

    def killed(self) -> bool:
        return self.kill_path.exists()

    def balance(self) -> float | None:
        try:
            b = self.client.balance_dollars()
        except KalshiError as e:
            if getattr(e, "status", None) in (401, 403):
                self.auth_ok = False
            log(f"LIVE balance check failed: {e}")
            return None
        self.auth_ok, self.last_balance = True, b
        return b

    def can_buy(self, event_ticker: str, cost: float) -> tuple[bool, str]:
        if self.killed():
            return False, "KILL file present"
        if not self.recon_ok:
            return False, "positions not reconciled this cycle"
        if event_ticker in self.blocked_events:
            return False, f"game blocked: {self.blocked_events[event_ticker]}"
        b = self.balance()
        if b is None:
            return False, "balance unavailable"
        if b - cost < self.floor - 1e-9:
            return False, f"balance floor: ${b:.2f} - ${cost:.2f} < ${self.floor:.2f}"
        return True, f"balance ${b:.2f}"

    def can_sell(self) -> tuple[bool, str]:
        if self.killed():
            return False, "KILL file present"
        if not self.recon_ok:
            return False, "positions not reconciled this cycle"
        return True, "ok"


def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


class LivePortfolio(Portfolio):
    def __init__(self, spec, cfg, root: Path, client, guard: Guard):
        super().__init__(spec, cfg, root)
        self.client, self.guard = client, guard
        self.ledger.mode = "LIVE"
        self.ledger.state.setdefault("intents", {})
        self.ledger.state["pending"] = {}           # live variants never use simulated maker bids
        self.ledger.save()

    @property
    def intents(self) -> dict:
        return self.ledger.state["intents"]

    def log(self, msg):
        log(f"[LIVE {self.spec.name}] {msg}")

    def busy_events(self) -> set:
        return {i["event_ticker"] for i in self.intents.values()}

    # ---------------- intents / fills ----------------
    def _new_intent(self, kind, ticker, **data) -> str:
        coid = str(uuid.uuid4())
        self.intents[coid] = {"kind": kind, "ticker": ticker, "client_order_id": coid, "created_at": time.time(),
                              "order_id": None, **data}
        self.ledger.save()            # persisted BEFORE the POST so a crash can never lead to a blind re-send
        return coid

    def _read_fills(self, order_id: str, expect: float) -> dict | None:
        """Sum the order's fills, waiting briefly for them to match the order's own fill count."""
        fr = None
        for i in range(FILL_POLL_TRIES):
            try:
                fr = self.client.fills_for_order(order_id)
            except KalshiError as e:
                self.log(f"fills lookup failed for {order_id}: {e}")
                fr = None
            if fr is not None and fr["count"] + 1e-6 >= expect:
                return fr
            if i + 1 < FILL_POLL_TRIES:
                time.sleep(1.0)
        return None

    def resolve_intent(self, coid: str, resp: dict | None = None) -> str:
        """Turn an intent into booked fills. Returns a status string; leaves the intent in place if unknown."""
        it = self.intents.get(coid)
        if it is None:
            return "gone"
        expect = None
        order = None
        if resp and resp.get("order_id"):
            it["order_id"] = resp["order_id"]
            expect = _f(resp.get("fill_count", resp.get("fill_count_fp")), None)
        if not it.get("order_id"):
            try:
                order = self.client.find_order_by_client_id(it["ticker"], coid)
            except KalshiError as e:
                return f"unknown (order lookup failed: {e})"
            if order is None:
                if time.time() - it["created_at"] > INTENT_LOOKUP_GRACE_SEC:
                    self.log(f"intent {coid[:8]} {it['kind']} {it['ticker']}: no such order on Kalshi -> never placed")
                    self.ledger.event("intent_dropped", intent=it)
                    del self.intents[coid]
                    self.ledger.save()
                    return "dropped (never placed)"
                return "unknown (order not visible yet)"
            it["order_id"] = order.get("order_id")
            expect = _f(order.get("fill_count_fp", order.get("fill_count")), None)
            st = order.get("status", "")
            if st in ("resting", "pending"):   # should not happen for IOC; never leave anything resting
                try:
                    self.client._signed("DELETE", f"/portfolio/events/orders/{it['order_id']}",
                                        params={"market_ticker": it["ticker"]})
                except KalshiError as e:
                    return f"unknown (resting order cancel failed: {e})"
                return "unknown (cancelled a resting remainder; re-check next cycle)"
        self.ledger.save()
        if expect is None:              # response had no fill count: ask the exchange for the order itself
            try:
                order = self.client.get_order(it["order_id"])
            except KalshiError as e:
                return f"unknown (order {it['order_id']} lookup failed: {e})"
            if not order:
                return f"unknown (order {it['order_id']} not visible yet)"
            if order.get("status") in ("resting", "pending"):
                return f"unknown (order {it['order_id']} still {order.get('status')})"
            expect = _f(order.get("fill_count_fp", order.get("fill_count")), None)
            if expect is None:
                return f"unknown (order {it['order_id']} has no fill count)"
        fr = self._read_fills(it["order_id"], expect)
        if fr is None:
            return f"unknown (fills for order {it['order_id']} not visible yet / < fill_count {expect:g})"
        n = fr["count"]
        if it["kind"] == "buy":
            self._book_buy(it, fr)
        else:
            self._book_sell(it, fr)
        self.ledger.event(f"{it['kind']}_result", client_order_id=coid, order_id=it["order_id"], ticker=it["ticker"],
                          requested=it["contracts"], filled=n, yes_value=fr["yes_value"], fees=fr["fees"])
        del self.intents[coid]
        self.ledger.save()
        return f"filled {n:g}/{it['contracts']}"

    def _book_buy(self, it, fr):
        n = fr["count"]
        if n <= 0:
            self.log(f"BUY IOC {it['ticker']} x{it['contracts']} @ {it['limit']:.2f}: NOT FILLED (order {it['order_id']})")
            return
        avg = fr["yes_value"] / n
        cost = round(fr["yes_value"] + fr["fees"], 4)
        L = self.ledger
        if it["ticker"] in L.positions:                     # merge (should not happen: one entry per game)
            p = L.positions[it["ticker"]]
            p["contracts"] += n
            p["entry_cost"] = round(p["entry_cost"] + cost, 4)
            p["entry_fee"] = round(p["entry_fee"] + fr["fees"], 4)
            p["entry_price"] = round((p["entry_cost"] - p["entry_fee"]) / p["contracts"], 4)
            L.add_spend(cost, it.get("day"))
            L.save()
        else:
            L.open_position(it["ticker"], it["team"], n, round(avg, 4), round(fr["fees"], 4), cost, it["fair"],
                            round(it["fair"] - avg, 4), it["event_ticker"], reason="|".join(it.get("tags", [])),
                            tags=it.get("tags", []), score=it.get("score"), entry_kind="taker_ioc",
                            raw_edge_c=it.get("raw_edge_c"), net_edge_c=it.get("net_edge_c"),
                            order_id=it["order_id"], client_order_id=it["client_order_id"], requested=it["contracts"],
                            limit_price=it["limit"])
            if it.get("day") and it["day"] != L.today():    # spend belongs to the day the order was sent
                L.add_spend(-cost)
                L.add_spend(cost, it["day"])
                L.save()
        self.log(f"BOUGHT {n:g}/{it['contracts']}x {it['ticker']} avg {avg:.4f} (limit {it['limit']:.2f}) "
                 f"fees ${fr['fees']:.4f} cost ${cost:.2f} order {it['order_id']}")

    def _book_sell(self, it, fr):
        L = self.ledger
        n = fr["count"]
        pos = L.positions.get(it["ticker"])
        if n <= 0 or pos is None:
            self.log(f"SELL IOC {it['ticker']} x{it['contracts']} @ {it['limit']:.2f}: NOT FILLED")
            return
        n = min(n, pos["contracts"])
        avg = fr["yes_value"] / fr["count"]
        proceeds = round(n * avg - fr["fees"], 4)
        reason = it.get("reason", "take_profit")
        if n >= pos["contracts"] - 1e-9:
            pnl = L.close_position(it["ticker"], round(avg, 4), round(fr["fees"], 4), proceeds, reason)
        else:
            frac = n / pos["contracts"]
            part_cost = round(pos["entry_cost"] * frac, 4)
            pnl = round(proceeds - part_cost, 4)
            L.state["realized_pnl"] = round(L.state["realized_pnl"] + pnl, 4)
            L.state["closed"].append({**pos, "ticker": it["ticker"], "contracts": n, "entry_cost": part_cost,
                                      "entry_fee": round(pos["entry_fee"] * frac, 4), "exit_price": round(avg, 4),
                                      "exit_fee": round(fr["fees"], 4), "proceeds": proceeds, "pnl": pnl,
                                      "reason": reason + "_partial", "closed_at": time.time()})
            pos["contracts"] = pos["contracts"] - n
            pos["entry_cost"] = round(pos["entry_cost"] - part_cost, 4)
            pos["entry_fee"] = round(pos["entry_fee"] * (1 - frac), 4)
            L._csv({"time_local": datetime.now().astimezone().isoformat(timespec="seconds"), "mode": "LIVE",
                    "action": "SELL_YES_PARTIAL", "ticker": it["ticker"], "team": pos["team"], "contracts": n,
                    "price": round(avg, 4), "fee": fr["fees"], "cash": proceeds, "reason": reason, "realized_pnl": pnl})
            L.save()
        self.log(f"SOLD {n:g}/{it['contracts']}x {it['ticker']} avg {avg:.4f} (limit {it['limit']:.2f}) "
                 f"proceeds ${proceeds:.2f} pnl ${pnl:+.2f} order {it['order_id']}")

    def resolve_all(self):
        for coid in list(self.intents):
            st = self.resolve_intent(coid)
            if not st.startswith("filled") and st != "gone":
                self.log(f"intent {coid[:8]} {self.intents.get(coid, {}).get('ticker', '')}: {st}")

    # ---------------- orders ----------------
    def _send(self, coid, ticker, book_side, count, price, reduce_only) -> str:
        if self.guard.killed():                       # last-moment check
            del self.intents[coid]
            self.ledger.save()
            return "KILL file present - not sent"
        try:
            resp = self.client.place_ioc(ticker, book_side, int(count), price, reduce_only=reduce_only,
                                         client_order_id=coid)
        except KalshiError as e:
            if getattr(e, "status", None) in (401, 403):
                self.guard.auth_ok = False
            self.log(f"ORDER ERROR {book_side} {ticker}: {e} -> will reconcile by client_order_id, no re-send")
            self.ledger.event("order_error", client_order_id=coid, ticker=ticker, error=str(e)[:300])
            return f"order error (reconciling): {str(e)[:80]}"
        self.ledger.event("order_sent", client_order_id=coid, ticker=ticker, side=book_side, count=count,
                          price=price, reduce_only=reduce_only, response=resp)
        return self.resolve_intent(coid, resp)

    def _try_enter(self, e, row, held_events) -> str:
        L, cfg = self.ledger, self.cfg
        if e["event_ticker"] in held_events or e["event_ticker"] in self.busy_events():
            return "skip: already holding/ordering this game"
        cool = cfg.reentry_cooldown_min * 60
        if any(c.get("event_ticker") == e["event_ticker"] and time.time() - c.get("closed_at", 0) < cool
               for c in L.state["closed"]):
            return f"skip: exited this game < {cfg.reentry_cooldown_min} min ago"
        n_buys = sum(1 for i in self.intents.values() if i["kind"] == "buy")
        if len(L.positions) + n_buys >= cfg.max_open_positions:
            return f"skip: max_open_positions={cfg.max_open_positions}"
        if e["cost"] > cfg.trade_size_usd + 1e-9:
            return f"skip: cost ${e['cost']:.2f} > trade size ${cfg.trade_size_usd}"
        if L.spent_today() + e["cost"] > cfg.max_daily_spend_usd + 1e-9:
            return f"skip: daily spend ${L.spent_today():.2f}+${e['cost']:.2f} > ${cfg.max_daily_spend_usd}"
        ok, why = self.guard.can_buy(e["event_ticker"], e["cost"])
        if not ok:
            self.log(f"entry qualified on {e['ticker']} but not sent: {why}")
            return f"skip: {why}"
        held_events.add(e["event_ticker"])
        limit = e["price"]                           # the ask the strategy evaluated; never above it
        coid = self._new_intent("buy", e["ticker"], event_ticker=e["event_ticker"], team=e["team"], fair=e["fair"],
                                contracts=int(e["contracts"]), limit=limit, est_cost=e["cost"], day=L.today(),
                                tags=e["tags"], score=e["score"], raw_edge_c=e["raw_edge_c"],
                                net_edge_c=e["net_edge_c"])
        self.log(f"SENDING BUY IOC {int(e['contracts'])}x {e['ticker']} @ <= {limit:.2f} fair={e['fair']:.3f} "
                 f"score {e['score']:+.1f}c est cost ${e['cost']:.2f} ({why})")
        return "LIVE BUY " + self._send(coid, e["ticker"], "bid", int(e["contracts"]), limit, False)

    def manage_exits(self, get_market):
        L = self.ledger
        busy = {i["ticker"] for i in self.intents.values()}
        for ticker, pos in list(L.positions.items()):
            mk = get_market(ticker)
            if mk is None:
                continue
            if mk.status in SETTLED_STATUSES and mk.result in ("yes", "no"):
                payout = float(pos["contracts"]) if mk.result == "yes" else 0.0
                pnl = L.close_position(ticker, 1.0 if mk.result == "yes" else 0.0, 0.0, payout, f"settled_{mk.result}")
                self.log(f"SETTLED {ticker} result={mk.result} pnl=${pnl:+.2f}")
                continue
            if not self.spec.take_profit or ticker in busy or mk.status not in ("active", "open"):
                continue
            n = int(pos["contracts"])
            if n <= 0:
                continue
            do_exit, reason, value = exit_signal(pos["entry_cost"], n, mk.yes_bid, self.cfg)
            if not do_exit:
                continue
            ok, why = self.guard.can_sell()
            if not ok:
                self.log(f"take-profit on {ticker} but not sent: {why}")
                continue
            coid = self._new_intent("sell", ticker, event_ticker=pos["event_ticker"], contracts=n, limit=mk.yes_bid,
                                    reason=reason)
            self.log(f"SENDING SELL IOC reduce-only {n}x {ticker} @ >= {mk.yes_bid:.2f} ({reason})")
            self.log("LIVE SELL " + self._send(coid, ticker, "ask", n, mk.yes_bid, True))


class LiveEngine(Engine):
    def __init__(self, cfg, client=None, specs=None, live_root: str = "data/live",
                 paper_odds_cache: str | None = None):
        root = cfg.path(live_root)
        lcfg = replace(cfg, contrarian_enabled=False,          # F and D never use contrarian signals
                       shared_dir=str(root / "engine"), variants_dir=str(root))
        names = set(LIVE_VARIANTS)
        # Live trading uses a 0c net-edge floor (paper variants keep their configured floors).
        # (still never buys above sportsbook fair after fees + half spread; D keeps its 5c score rule).
        specs = specs or [replace(v, edge_floor_cents=LIVE_EDGE_FLOOR_CENTS) for v in VARIANTS if v.name in names]
        assert {s.name for s in specs} <= names, "only approved variants may trade live"
        Engine.__init__(self, lcfg, specs=specs)         # data pipeline; portfolios replaced by live ones below
        self.root = root
        if client is None:
            key_id = (os.environ.get("KALSHI_API_KEY_ID") or "").strip()
            pem = load_private_key_from_env()
            if not key_id or not pem:
                raise SystemExit("Live mode needs KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY in the environment.")
            client = KalshiClient(cfg.kalshi_base_url, cfg.kalshi_trade_base_url, key_id, pem)
        self.client = client
        self.kalshi = client                               # public market data + signed calls
        self.guard = Guard(client, root / "KILL", cfg.live_balance_floor_usd)
        self.portfolios = [LivePortfolio(s, lcfg, root, client, self.guard) for s in specs]
        self.odds_cache_path = Path(paper_odds_cache) if paper_odds_cache else cfg.path(cfg.shared_dir) / "odds_cache.pkl"
        self._odds_mtime = None
        self.odds_cache = {}
        self.reload_odds()
        self.last_recon = None
        self.kill_logged = False
        self._mismatch: dict[str, int] = {}

    # ----- odds: read-only from the paper runner's cache (never spends quota) -----
    def reload_odds(self):
        try:
            mt = self.odds_cache_path.stat().st_mtime
        except FileNotFoundError:
            return
        if mt == self._odds_mtime:
            return
        try:
            self.odds_cache = pickle.loads(self.odds_cache_path.read_bytes())
            self._odds_mtime = mt
        except Exception as e:
            log(f"LIVE: paper odds cache unreadable ({type(e).__name__}); keeping previous")

    def refresh_odds(self, need, force=False):
        self.reload_odds()

    # ----- reconciliation -----
    def reconcile(self) -> bool:
        g = self.guard
        g.recon_ok = False
        for p in self.portfolios:
            try:
                p.resolve_all()
            except Exception as e:
                p.log(f"intent resolution error: {type(e).__name__}: {e}")
        try:
            acct = self.client.positions(SPORTS_PREFIXES)
        except KalshiError as e:
            if getattr(e, "status", None) in (401, 403):
                g.auth_ok = False
            log(f"LIVE reconcile: positions unavailable ({e}) -> no orders this cycle")
            return False
        g.auth_ok = True
        tracked: dict[str, float] = {}
        events: dict[str, str] = {}
        for p in self.portfolios:
            for t, pos in p.ledger.positions.items():
                tracked[t] = tracked.get(t, 0.0) + float(pos["contracts"])
                events[t] = pos["event_ticker"]
        pending = {i["ticker"] for p in self.portfolios for i in p.intents.values()}
        blocked = {}
        for t, q in acct.items():
            ev = t.rsplit("-", 1)[0]
            if q < 0:
                blocked[ev] = f"account holds NO on {t} ({q:g})"
            elif q > tracked.get(t, 0.0) + 1e-6 and t not in pending:
                blocked[ev] = f"untracked contracts on {t}: account {q:g} vs bot {tracked.get(t, 0.0):g}"
        for t, q in tracked.items():
            have = max(0.0, acct.get(t, 0.0))
            if have + 1e-6 >= q or t in pending:
                continue
            try:
                mk = self.client.market(t)
            except KalshiError:
                mk = None
            if mk is None or mk.status not in ("active", "open"):
                continue          # game over / settling: settlement booking happens via the market result
            self._mismatch[t] = self._mismatch.get(t, 0) + 1
            blocked[events[t]] = f"bot/exchange mismatch on {t}"
            if self._mismatch[t] < 2:
                log(f"LIVE reconcile: {t} bot tracks {q:g} but account holds {have:g}; re-checking next cycle")
                continue
            short = q - have
            log(f"LIVE reconcile WARNING: {t} bot tracks {q:g} but account holds {have:g}; trimming bot positions")
            for p in sorted(self.portfolios, key=lambda p: p.spec.take_profit, reverse=True):   # D before F
                pos = p.ledger.positions.get(t)
                if not pos or short <= 1e-9:
                    continue
                cut = min(short, pos["contracts"])
                frac = cut / pos["contracts"]
                p.ledger.event("reconcile_trim", ticker=t, cut=cut, of=pos["contracts"])
                if cut >= pos["contracts"] - 1e-9:
                    p.ledger.close_position(t, 0.0, 0.0, 0.0, "reconcile_missing_on_exchange")
                else:
                    pos["contracts"] -= cut
                    pos["entry_cost"] = round(pos["entry_cost"] * (1 - frac), 4)
                    p.ledger.save()
                short -= cut
            blocked[events[t]] = f"bot/exchange mismatch on {t}"
        self._mismatch = {t: c for t, c in self._mismatch.items()
                          if t in tracked and acct.get(t, 0.0) + 1e-6 < tracked[t]}
        for ev, why in blocked.items():
            if g.blocked_events.get(ev) != why:
                log(f"LIVE reconcile: blocking new buys on {ev}: {why}")
        g.blocked_events = blocked
        g.recon_ok = True
        self.last_recon = {"at": time.time(), "account_sports_positions": acct, "tracked": tracked,
                           "blocked": blocked}
        return True

    # ----- cycle -----
    def step(self, force_odds: bool = False):
        self.cycle += 1
        self._trade_cache = {}
        now = datetime.now(timezone.utc)
        killed = self.guard.killed()
        if killed and not self.kill_logged:
            log(f"LIVE: KILL file present ({self.guard.kill_path}) -> no new orders until it is removed")
        self.kill_logged = killed
        markets, events = self.kalshi_snapshot()
        by_ticker = {m.ticker: m for m in markets}
        need = self.sports_needing_odds(events, now)
        self.refresh_odds(need)
        self.guard.balance()
        self.reconcile()
        rows = self.analyze(events, now)
        self.update_closing(rows, now)
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

        decisions = {}
        for p in self.portfolios:
            try:
                p.manage_exits(get_market)
                decisions[p.spec.name] = [] if killed else p.consider(rows)
            except Exception as e:
                p.log(f"variant error: {type(e).__name__}: {e}")
        ages = {s: int(time.time() - v[0]) for s, v in self.odds_cache.items()}
        log(f"LIVE cycle {self.cycle}: {len(markets)} markets/{len(events)} games, {len(rows)} priced, odds age "
            f"{ages}, balance ${self.guard.last_balance if self.guard.last_balance is not None else float('nan'):.2f}"
            f", recon {'ok' if self.guard.recon_ok else 'FAILED'}{', KILL' if killed else ''}")
        for p in self.portfolios:
            L = p.ledger
            p.log(f"open {len(L.positions)} intents {len(p.intents)} | spent today ${L.spent_today():.2f} | "
                  f"realized ${L.state['realized_pnl']:+.2f}")
            L.event("cycle", decisions=[d for d in decisions.get(p.spec.name, []) if d.get("ok")])
        self.write_marks(by_ticker, extra)
        self.heartbeat()
        return {"rows": rows, "decisions": decisions}, any(need.values())

    def heartbeat(self):
        _write_json(self.root / "heartbeat.json", {
            "at": time.time(), "at_local": datetime.now().astimezone().isoformat(timespec="seconds"),
            "cycle": self.cycle, "auth_ok": self.guard.auth_ok, "balance": self.guard.last_balance,
            "recon_ok": self.guard.recon_ok, "kill": self.guard.killed(),
            "blocked_events": self.guard.blocked_events,
            "odds_age_s": {s: int(time.time() - v[0]) for s, v in self.odds_cache.items()},
            "variants": [p.spec.name for p in self.portfolios], "pid": os.getpid()})

    def run(self):
        while True:
            any_live = False
            try:
                _, any_live = self.step()
            except Exception as e:      # keep the loop alive; never crash-loop into orders
                log(f"LIVE unexpected error: {type(e).__name__}: {e}")
                try:
                    self.heartbeat()
                except Exception:
                    pass
            time.sleep(self.cfg.interval_live_sec if any_live else self.cfg.interval_idle_sec)


BANNER = """
==========================================================================
  KALSHI SPORTS BOT - LIVE TRADING (REAL MONEY)
  variants: {variants}
  ${size:.0f}/trade, max {maxpos} open, ${daily:.0f}/day per variant; balance floor ${floor:.0f}
  stop new orders:  touch {kill}
==========================================================================
"""


def main(argv=None):
    import argparse
    import sys
    from .config import ROOT, load_config
    from .envfile import load_dotenv

    ap = argparse.ArgumentParser(description="LIVE runner for F_hold_to_settle + D_no_contrarian (real orders)")
    ap.add_argument("--live", action="store_true", help="required: acknowledge that real orders will be placed")
    ap.add_argument("--once", action="store_true", help="run one cycle and exit")
    ap.add_argument("--config", default=None)
    args = ap.parse_args(argv)
    if not args.live:
        print("Refusing to start: this runner places REAL orders. Pass --live to confirm.", file=sys.stderr)
        return 2
    load_dotenv(ROOT / ".env")
    cfg = load_config(args.config)
    eng = LiveEngine(cfg)
    print(BANNER.format(variants=", ".join(p.spec.name for p in eng.portfolios), size=cfg.trade_size_usd,
                        maxpos=cfg.max_open_positions, daily=cfg.max_daily_spend_usd,
                        floor=cfg.live_balance_floor_usd, kill=eng.guard.kill_path), flush=True)
    bal = eng.guard.balance()
    if bal is None:
        log("LIVE: could not authenticate / read balance at startup -> exiting without trading")
        return 3
    log(f"LIVE MODE authenticated. Kalshi balance ${bal:.2f}. Odds source: {eng.odds_cache_path} (read-only)")
    if not eng.reconcile():
        log("LIVE: startup reconciliation failed -> will retry each cycle; no orders until it succeeds")
    else:
        log(f"LIVE startup reconcile ok: account NFL/NBA positions {eng.last_recon['account_sports_positions']}, "
            f"bot-tracked {eng.last_recon['tracked']}")
    try:
        if args.once:
            eng.step()
        else:
            eng.run()
    except KeyboardInterrupt:
        log("stopped.")
    return 0
