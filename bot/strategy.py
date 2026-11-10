"""Pure strategy logic: game matching, fees, entry/exit signals, odds-quota budgeting."""
from __future__ import annotations

import calendar
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .kalshi import KalshiMarket
from .odds import GameOdds
from .teams import odds_name_to_abbr, subtitle_matches_name

MAX_START_DIFF = timedelta(hours=12)


# ---------- matching ----------
def team_for_market(game: GameOdds, mk: KalshiMarket) -> str | None:
    """Return the Odds API team name this Kalshi market's YES side pays on, or None."""
    for name in (game.home, game.away):
        if odds_name_to_abbr(game.sport, name) == mk.team_abbr:
            return name
    for name in (game.home, game.away):  # fallback on Kalshi's label ("Los Angeles L")
        if subtitle_matches_name(mk.team_label, name):
            return name
    return None


def match_game(game: GameOdds, events: dict[str, list[KalshiMarket]]) -> list[tuple[KalshiMarket, str]]:
    """Find the Kalshi event for an Odds API game. Requires both teams to map and the
    scheduled start to be within 12h (so back-to-back series games don't cross-match)."""
    best = None
    for ev_ticker, mks in events.items():
        if not mks or mks[0].sport != game.sport:
            continue
        pairs = [(m, team_for_market(game, m)) for m in mks]
        names = {n for _, n in pairs if n}
        if names != {game.home, game.away}:
            continue
        st = mks[0].start_time
        diff = abs(st - game.commence) if st else timedelta(days=99)
        if diff > MAX_START_DIFF:
            continue
        if best is None or diff < best[0]:
            best = (diff, [(m, n) for m, n in pairs if n])
    return best[1] if best else []


# ---------- fees ----------
def taker_fee(contracts: float, price: float, rate: float = 0.07) -> float:
    """Kalshi general taker fee: round_up_to_cent(0.07 * C * P * (1-P))."""
    if contracts <= 0 or price <= 0 or price >= 1:
        return 0.0
    return math.ceil(round(rate * contracts * price * (1 - price) * 100, 6)) / 100.0


def maker_fee(contracts: float, price: float, rate: float = 0.0175) -> float:
    """Kalshi maker fee on series with maker fees (NFL/NBA game markets): round_up(0.0175*C*P*(1-P))."""
    return taker_fee(contracts, price, rate)


# ---------- signals ----------
@dataclass
class EntrySignal:
    ticker: str
    team: str
    fair: float
    ask: float
    edge: float
    contracts: int
    cost: float          # contracts*ask (+ fee if include_fees)
    fee: float


def contracts_for_budget(budget: float, ask: float, include_fees: bool) -> int:
    if ask <= 0:
        return 0
    n = int(budget // ask)
    while n > 0 and n * ask + (taker_fee(n, ask) if include_fees else 0) > budget + 1e-9:
        n -= 1
    return n


def entry_signal(mk: KalshiMarket, team: str, fair: float, cfg, budget: float | None = None,
                 require_edge: bool = True) -> tuple[EntrySignal | None, str]:
    """Return (signal, reason). Signal is None when we should not buy.
    require_edge=False skips the pure odds-edge gate (the combined score decides instead)."""
    ask = mk.yes_ask
    if mk.status not in ("active", "open"):
        return None, f"status={mk.status}"
    if ask is None or ask <= 0:
        return None, "no ask"
    if not (cfg.min_price <= ask <= cfg.max_price):
        return None, f"ask {ask:.2f} outside [{cfg.min_price},{cfg.max_price}]"
    if mk.spread is not None and mk.spread > cfg.max_spread + 1e-9:
        return None, f"spread {mk.spread:.2f} > {cfg.max_spread}"
    edge = fair - ask
    if require_edge and edge + 1e-9 < cfg.edge_cents / 100.0:
        return None, f"edge {edge*100:+.1f}c < {cfg.edge_cents}c"
    size = min(cfg.trade_size_usd, budget) if budget is not None else cfg.trade_size_usd
    n = contracts_for_budget(size, ask, cfg.include_fees)
    if mk.yes_ask_size is not None:
        n = min(n, int(mk.yes_ask_size))
    if n <= 0:
        return None, "size rounds to 0 contracts (budget/liquidity)"
    fee = taker_fee(n, ask) if cfg.include_fees else 0.0
    return EntrySignal(mk.ticker, team, fair, ask, edge, n, round(n * ask + fee, 4), fee), "edge"


def exit_signal(entry_cost: float, contracts: int, bid: float | None, cfg) -> tuple[bool, str, float]:
    """Decide whether to sell at the current YES bid. Returns (exit?, reason, exit_value)."""
    if bid is None or bid <= 0:
        return False, "no bid", 0.0
    fee = taker_fee(contracts, bid) if cfg.include_fees else 0.0
    value = contracts * bid - fee
    pnl_pct = (value - entry_cost) / entry_cost * 100 if entry_cost > 0 else 0.0
    if pnl_pct + 1e-9 >= cfg.take_profit_pct:
        return True, f"take_profit {pnl_pct:+.1f}%", value
    if cfg.stop_loss_pct > 0 and pnl_pct <= -cfg.stop_loss_pct:
        return True, f"stop_loss {pnl_pct:+.1f}%", value
    return False, f"hold {pnl_pct:+.1f}%", value


# ---------- Odds API quota budgeting ----------
def odds_min_interval(remaining: int | None, reserve: int, now: datetime | None = None,
                      floor_sec: int = 60, until: datetime | None = None) -> float:
    """Seconds to wait between paid Odds API calls so the remaining quota lasts until the
    1st of next month (free tier resets monthly). Returns inf when only the reserve is left."""
    if remaining is None:
        return float(floor_sec)
    usable = remaining - reserve
    if usable <= 0:
        return float("inf")
    now = now or datetime.now(timezone.utc)
    days = calendar.monthrange(now.year, now.month)[1]
    month_end = now.replace(day=days, hour=23, minute=59, second=59, microsecond=0)
    end = until if (until is not None and until > month_end) else month_end
    secs_left = max((end - now).total_seconds(), 3600)
    return max(float(floor_sec), secs_left / usable)
