"""Kalshi NFL/NBA sports bot: sportsbook-consensus fair value vs Kalshi YES ask."""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone

from .config import ROOT, load_config
from .envfile import load_dotenv
from .kalshi import KalshiClient, KalshiError, group_events, load_private_key_from_env
from .ledger import Ledger
from .odds import MissingOddsKey, OddsAPIError, OddsClient
from .espn import ESPNNews
from .signals import Demand, combine, contrarian_score, game_demand, news_score, volume_gate
from .strategy import entry_signal, exit_signal, match_game, odds_min_interval

SETTLED_STATUSES = {"finalized", "settled", "determined"}


def _parse_until(s: str):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except ValueError:
        return None


def log(msg: str):
    print(f"[{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}] {msg}", flush=True)


class Bot:
    def __init__(self, cfg, live: bool = False):
        self.cfg, self.live = cfg, live
        mode = "LIVE" if live else "DRY_RUN"
        self.ledger = Ledger(cfg.path(cfg.state_file), cfg.path(cfg.trade_log_csv), cfg.path(cfg.event_log_jsonl), mode)
        key_id, pem = None, None
        if live:
            key_id = os.environ.get("KALSHI_API_KEY_ID")
            pem = load_private_key_from_env()
            if not key_id or not pem:
                raise SystemExit("Live mode needs KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH (or KALSHI_PRIVATE_KEY).")
        self.kalshi = KalshiClient(cfg.kalshi_base_url, cfg.kalshi_trade_base_url, key_id, pem)
        self.odds = OddsClient(os.environ.get("ODDS_API_KEY"), cfg.odds_base_url, cfg.regions)
        self.odds.remaining = self.ledger.state.get("odds_remaining")
        self.odds_cache: dict[str, tuple[float, list]] = {}   # sport -> (fetched_at, [GameOdds])
        self.micro_cache: dict[str, tuple[float, Demand]] = {}
        self.micro_raw: dict[str, dict] = {}     # ticker -> {"book": ..., "trades": [...]} from last fetch
        self.soon_sports: set[str] = set()
        self.news = ESPNNews(cfg) if cfg.news_enabled else None

    # ---------------- data ----------------
    def kalshi_snapshot(self):
        markets = []
        for s in self.cfg.series:
            markets += self.kalshi.open_markets(s)
        return markets, group_events(markets)

    def sports_needing_odds(self, events, now):
        need: dict[str, bool] = {}   # sport -> any live game
        window = timedelta(hours=self.cfg.pregame_window_hours)
        soon = timedelta(minutes=self.cfg.closing_soon_min)
        self.soon_sports = {m[0].sport for m in events.values()
                            if m[0].start_time and m[0].sport in self.cfg.sports and now < m[0].start_time <= now + soon}
        for mks in events.values():
            st = mks[0].start_time
            if not st or not mks[0].sport or mks[0].sport not in self.cfg.sports:
                continue
            if st <= now:
                need[mks[0].sport] = True
            elif st - now <= window:
                need.setdefault(mks[0].sport, False)
        return need

    def refresh_odds(self, need: dict[str, bool], force: bool = False):
        st = self.ledger.state
        last_paid = max([0.0] + [float(v) for v in st["odds_last_fetch"].values()])
        gap = odds_min_interval(self.odds.remaining, self.cfg.quota_reserve, until=_parse_until(self.cfg.odds_pace_until))
        if gap == float("inf"):
            log(f"Odds API quota at reserve ({self.odds.remaining} left) - not fetching odds.")
            return
        # live sports first, then sports with a game about to start (closing line), then the stalest
        order = sorted(need, key=lambda s: (not need[s], s not in self.soon_sports,
                                            float(st["odds_last_fetch"].get(s, 0))))
        for sport in order:
            if not force and time.time() - last_paid < gap:
                log(f"Odds budget: next paid call allowed in {int(gap - (time.time() - last_paid))}s "
                    f"(spacing {int(gap)}s so the quota lasts to {self.cfg.odds_pace_until or "month end"}; {self.odds.remaining} left).")
                return
            try:
                now = time.time()
                games = self.odds.h2h_odds(sport, now)
            except MissingOddsKey:
                raise
            except OddsAPIError as e:
                log(f"Odds API error for {sport}: {e}")
                return
            self.odds_cache[sport] = (now, games)
            st["odds_last_fetch"][sport] = now
            st["odds_remaining"] = self.odds.remaining
            last_paid = now
            self.ledger.save()
            log(f"Odds API: {sport} -> {len(games)} games (cost {self.odds.last_cost}, "
                f"used {self.odds.used}, remaining {self.odds.remaining})")
            self.ledger.event("odds_fetch", sport=sport, games=len(games), remaining=self.odds.remaining,
                              used=self.odds.used, cost=self.odds.last_cost)
            if not force:
                return   # at most one paid call per loop iteration

    # ---------------- trading ----------------
    def manage_exits(self, by_ticker):
        for ticker, pos in list(self.ledger.positions.items()):
            mk = by_ticker.get(ticker)
            if mk is None:
                try:
                    mk = self.kalshi.market(ticker)
                except KalshiError as e:
                    log(f"could not refresh {ticker}: {e}")
                    continue
            if mk.status in SETTLED_STATUSES and mk.result in ("yes", "no"):
                payout = float(pos["contracts"]) if mk.result == "yes" else 0.0
                pnl = self.ledger.close_position(ticker, 1.0 if mk.result == "yes" else 0.0, 0.0, payout, f"settled_{mk.result}")
                log(f"SETTLED {ticker} result={mk.result} pnl=${pnl:+.2f}")
                continue
            do_exit, reason, value = exit_signal(pos["entry_cost"], pos["contracts"], mk.yes_bid, self.cfg)
            if not do_exit:
                continue
            price, proceeds = mk.yes_bid, value
            fee = round(pos["contracts"] * mk.yes_bid - value, 4)
            if self.live:
                try:
                    r = self.kalshi.place_ioc(ticker, "ask", int(pos["contracts"]), mk.yes_bid, reduce_only=True)
                except KalshiError as e:
                    log(f"LIVE sell failed {ticker}: {e}")
                    continue
                filled = float(r.get("fill_count", 0))
                if filled < pos["contracts"]:
                    log(f"LIVE sell {ticker} only filled {filled}/{pos['contracts']}; keeping position open")
                    self.ledger.event("partial_exit", ticker=ticker, response=r)
                    if filled == 0:
                        continue
                    pos["contracts"] -= filled   # keep remainder (simple handling)
                    self.ledger.save()
                    continue
                price = float(r.get("average_fill_price", price))
                fee = float(r.get("average_fee_paid", 0)) * filled if r.get("average_fee_paid") else fee
                proceeds = filled * price - fee
            pnl = self.ledger.close_position(ticker, price, round(fee, 4), round(proceeds, 4), reason)
            log(f"{'SELL' if self.live else 'WOULD SELL'} {ticker} {pos['contracts']} @ {price:.2f} ({reason}) pnl=${pnl:+.2f}")

    def microstructure(self, mks):
        """Orderbooks + recent trades for both markets of a game (public, cached)."""
        key = mks[0].event_ticker
        hit = self.micro_cache.get(key)
        if hit and time.time() - hit[0] < self.cfg.microstructure_refresh_sec:
            return hit[1]
        books, trades = {}, {}
        since = int(time.time() - self.cfg.flow_window_hours * 3600)
        for m in mks:
            try:
                books[m.ticker] = self.kalshi.orderbook(m.ticker)
                trades[m.ticker] = self.kalshi.trades(m.ticker, since)
                self.micro_raw[m.ticker] = {"book": books[m.ticker], "trades": trades[m.ticker], "at": time.time()}
            except KalshiError as e:
                log(f"microstructure fetch failed {m.ticker}: {e}")
        demand = game_demand(mks, books, trades, self.cfg.book_window_dollars)
        self.micro_cache[key] = (time.time(), demand)
        return demand

    def find_entries(self, events, now) -> list[dict]:
        cfg = self.cfg
        report = []
        open_events = {p["event_ticker"] for p in self.ledger.positions.values()}
        vol = volume_gate(events, cfg)
        for sport, (fetched_at, games) in self.odds_cache.items():
            team_news = self.news.team_news(sport) if (cfg.news_enabled and self.news) else {}
            for g in games:
                pairs = match_game(g, events)
                if not pairs:
                    continue
                ev_ticker = pairs[0][0].event_ticker
                mks = events[ev_ticker]
                live = g.is_live(now)
                age = time.time() - fetched_at
                max_age = cfg.max_odds_age_live_sec if live else cfg.max_odds_age_pregame_sec
                v = vol.get(ev_ticker, {"pass": False, "why": "no volume data", "volume": 0, "volume_24h": 0,
                                        "open_interest": 0, "rank_pct": 100})
                game_block = None
                if not v["pass"]:
                    game_block = f"volume gate: {v['why']}"
                elif g.n_books < cfg.min_books:
                    game_block = f"only {g.n_books} books"
                elif age > max_age:
                    game_block = f"odds stale ({int(age)}s > {max_age}s)"
                elif live and not cfg.allow_live_entries:
                    game_block = "live entries disabled"
                demand = self.microstructure(mks) if (v["pass"] and cfg.contrarian_enabled) else Demand()
                fair_by_abbr = {mk.team_abbr: g.fair[team] for mk, team in pairs}
                row = {"sport": sport, "event": ev_ticker, "game": f"{g.away} @ {g.home}", "start": g.commence,
                       "live": live, "books": g.n_books, "odds_age_s": int(age), "volume": v, "markets": [],
                       "news": {a: [f.__dict__ for f in tn.flags] for a, tn in team_news.items()
                                if a in fair_by_abbr},
                       "demand": {"book": {k: round(x) for k, x in demand.book.items()},
                                  "flow": {k: round(x) for k, x in demand.flow.items()}}}
                candidates = []
                for mk, team in pairs:
                    opp_mk = next((m for m, _ in pairs if m.ticker != mk.ticker), None)
                    if opp_mk is None:
                        continue
                    fair = g.fair[team]
                    edge_c = (fair - mk.yes_ask) * 100 if mk.yes_ask else float("-inf")
                    if cfg.contrarian_enabled:
                        contra = contrarian_score(mk.team_abbr, opp_mk, fair_by_abbr[opp_mk.team_abbr], demand, cfg)
                        if not v["pass"]:   # orderbook/flow not fetched for gated games
                            contra["book_c"] = contra["flow_c"] = 0.0
                    else:
                        contra = {"total": 0.0}
                    news = news_score(mk.team_abbr, opp_mk.team_abbr, team_news, cfg) if cfg.news_enabled else \
                        {"total": 0.0, "neg_self": 0.0, "neg_opp": 0.0, "blocked": False}
                    ok, score, why = combine(edge_c, contra, news, cfg)
                    decision = game_block or why
                    sig = None
                    if ok and not game_block:
                        sig, fwhy = entry_signal(mk, team, fair, cfg, require_edge=False)
                        if not sig:
                            decision = f"filter: {fwhy}"
                    row["markets"].append({"ticker": mk.ticker, "team": team, "abbr": mk.team_abbr, "fair": fair,
                                           "bid": mk.yes_bid, "ask": mk.yes_ask, "edge_c": round(edge_c, 2),
                                           "contra": contra, "news": news, "score": score, "decision": decision})
                    if sig:
                        candidates.append((score, sig, mk))
                report.append(row)
                self.ledger.event("score", event=ev_ticker, game=row["game"], live=live, volume=v,
                                  demand=row["demand"], markets=row["markets"])
                if not candidates:
                    continue
                score, sig, mk = max(candidates, key=lambda c: c[0])   # never buy both sides of one game
                for m in row["markets"]:
                    if m["ticker"] == sig.ticker:
                        m["decision"] = f"{m['decision']} -> " + self.try_enter(sig, mk, open_events)
        return report

    def try_enter(self, sig, mk, open_events) -> str:
        L, cfg = self.ledger, self.cfg
        if sig.ticker in L.positions or mk.event_ticker in open_events:
            return "skip: already holding this game"
        cool = cfg.reentry_cooldown_min * 60
        if any(c.get("event_ticker") == mk.event_ticker and time.time() - c.get("closed_at", 0) < cool
               for c in L.state["closed"]):
            return f"skip: exited this game < {cfg.reentry_cooldown_min} min ago"
        if len(L.positions) >= cfg.max_open_positions:
            return f"skip: max_open_positions={cfg.max_open_positions}"
        if L.spent_today() + sig.cost > cfg.max_daily_spend_usd + 1e-9:
            return f"skip: daily spend ${L.spent_today():.2f}+${sig.cost:.2f} > ${cfg.max_daily_spend_usd}"
        contracts, price, fee, cost = sig.contracts, sig.ask, sig.fee, sig.cost
        if self.live:
            try:
                r = self.kalshi.place_ioc(sig.ticker, "bid", sig.contracts, sig.ask)
            except KalshiError as e:
                log(f"LIVE buy failed {sig.ticker}: {e}")
                return f"order error: {e}"
            filled = int(float(r.get("fill_count", 0)))
            self.ledger.event("order", ticker=sig.ticker, response=r)
            if filled == 0:
                return "IOC not filled"
            contracts = filled
            price = float(r.get("average_fill_price", sig.ask))
            fee = float(r.get("average_fee_paid", 0)) * filled if r.get("average_fee_paid") else 0.0
            cost = round(contracts * price + fee, 4)
        L.open_position(sig.ticker, sig.team, contracts, price, fee, cost, sig.fair, sig.edge, mk.event_ticker)
        open_events.add(mk.event_ticker)
        verb = "BOUGHT" if self.live else "WOULD BUY"
        log(f"{verb} {contracts}x {sig.ticker} ({sig.team}) @ {price:.2f}  fair={sig.fair:.3f} "
            f"edge={sig.edge*100:+.1f}c cost=${cost:.2f}")
        self.ledger.event("entry", ticker=sig.ticker, team=sig.team, contracts=contracts, price=price,
                          fair=sig.fair, edge=sig.edge, cost=cost)
        return f"{verb} {contracts} @ {price:.2f} (cost ${cost:.2f})"

    # ---------------- loop ----------------
    def step(self, force_odds: bool = False):
        now = datetime.now(timezone.utc)
        markets, events = self.kalshi_snapshot()
        by_ticker = {m.ticker: m for m in markets}
        need = self.sports_needing_odds(events, now)
        log(f"Kalshi: {len(markets)} open markets / {len(events)} games; sports needing odds: "
            f"{ {s: ('LIVE' if v else 'pregame') for s, v in need.items()} }")
        self.refresh_odds(need, force=force_odds)
        self.manage_exits(by_ticker)
        report = self.find_entries(events, now)
        self.mark_to_market(by_ticker)
        return report, any(need.values())

    def mark_to_market(self, by_ticker):
        unreal = 0.0
        for t, p in self.ledger.positions.items():
            mk = by_ticker.get(t)
            if mk and mk.yes_bid is not None:
                unreal += p["contracts"] * mk.yes_bid - p["entry_cost"]
        log(f"Positions open: {len(self.ledger.positions)} | spent today ${self.ledger.spent_today():.2f} | "
            f"realized P&L ${self.ledger.state['realized_pnl']:+.2f} | unrealized (at bid, pre-fee) ${unreal:+.2f}")

    def run(self):
        while True:
            try:
                _, any_live = self.step()
            except (KalshiError, OddsAPIError) as e:
                log(f"error: {e}")
                if isinstance(e, MissingOddsKey):
                    raise SystemExit(str(e))
                any_live = False
            except Exception as e:  # keep the loop alive; log the problem
                log(f"unexpected error: {type(e).__name__}: {e}")
                any_live = False
            time.sleep(self.cfg.interval_live_sec if any_live else self.cfg.interval_idle_sec)


def _f(x, fmt="{:.2f}"):
    return "--" if x is None else fmt.format(x)


def print_report(report):
    if not report:
        print("No Odds API games matched to Kalshi markets this pass.")
        return
    for row in sorted(report, key=lambda r: r["start"]):
        start = row["start"].astimezone().strftime("%a %b %d %I:%M %p %Z")
        v = row["volume"]
        print(f"\n{row['game']}  [{row['sport'].split('_')[-1].upper()}]  {start}{'  LIVE' if row['live'] else ''}"
              f"  books={row['books']}")
        print(f"   volume {v['volume']:,.0f} | 24h {v['volume_24h']:,.0f} | OI {v['open_interest']:,.0f} | "
              f"rank {v.get('rank','?')} -> {'PASS' if v['pass'] else 'FAIL'}"
              + (f"   bid-depth {row['demand']['book']}  taker-flow {row['demand']['flow']}"
                 if row["demand"]["book"] else ""))
        for abbr, flags in row["news"].items():
            for f in flags[:4]:
                print(f"   news {abbr}: {f['kind']} {f['who']} [{f['status']}] w={f['weight']} {f['note'][:90]}")
        for m in row["markets"]:
            c, n = m["contra"], m["news"]
            print(f"   {m['abbr']:<4} fair={m['fair']:.3f} bid={_f(m['bid'])} ask={_f(m['ask'])} | edge {m['edge_c']:+5.1f}"
                  f" + contra {c['total']:+.1f} (book {c.get('book_c',0):.1f}, flow {c.get('flow_c',0):.1f}, "
                  f"overpay {c.get('overpay_c',0):.1f}, drift {c.get('drift_c',0):.1f})"
                  f" + news {n['total']:+.1f} = {m['score']:+5.1f}c  -> {m['decision']}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Kalshi NFL/NBA sportsbook-edge bot")
    ap.add_argument("--config", default=None)
    ap.add_argument("--once", action="store_true", help="run one pass (fetches odds now, within quota) and exit")
    ap.add_argument("--live", action="store_true", help="LIVE trading. Also requires DRY_RUN=false.")
    args = ap.parse_args(argv)

    load_dotenv(ROOT / ".env")
    cfg = load_config(args.config)
    live = False
    if args.live:
        if cfg.dry_run:
            raise SystemExit("--live given but dry_run is still true. Set DRY_RUN=false (env) or "
                             "[mode] dry_run=false in config.toml as well. Refusing to trade.")
        live = True
    elif not cfg.dry_run:
        log("dry_run=false but --live not given -> staying in DRY_RUN.")

    try:
        bot = Bot(cfg, live=live)
    except MissingOddsKey as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    if live:
        bal = bot.kalshi.balance()
        log(f"LIVE MODE. Kalshi balance ${float(bal.get('balance', 0))/100:.2f}")
    else:
        log("DRY_RUN mode - no orders will be sent.")
    log(f"edge>={cfg.edge_cents}c  size=${cfg.trade_size_usd}  TP={cfg.take_profit_pct}%  "
        f"SL={'off' if cfg.stop_loss_pct <= 0 else str(cfg.stop_loss_pct)+'%'}  max_pos={cfg.max_open_positions}  "
        f"max_daily=${cfg.max_daily_spend_usd}")
    try:
        if args.once:
            report, _ = bot.step(force_odds=True)
            print_report(report)
        else:
            bot.run()
    except MissingOddsKey as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        log("stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
