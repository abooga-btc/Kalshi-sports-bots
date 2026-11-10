"""Load config.toml and apply UPPER_CASE environment-variable overrides."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _coerce(raw: str, current):
    if isinstance(current, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(current, int):
        return int(float(raw))
    if isinstance(current, float):
        return float(raw)
    if isinstance(current, list):
        return [x.strip() for x in raw.split(",") if x.strip()]
    return raw


@dataclass
class Config:
    dry_run: bool = True
    kalshi_base_url: str = "https://api.elections.kalshi.com/trade-api/v2"
    kalshi_trade_base_url: str = "https://api.elections.kalshi.com/trade-api/v2"
    series: list = field(default_factory=lambda: ["KXNFLGAME", "KXNBAGAME"])
    odds_base_url: str = "https://api.the-odds-api.com/v4"
    sports: list = field(default_factory=lambda: ["americanfootball_nfl", "basketball_nba"])
    regions: str = "us"
    min_books: int = 3
    monthly_quota: int = 500
    quota_reserve: int = 20
    pregame_window_hours: float = 36.0
    max_odds_age_pregame_sec: int = 3600
    max_odds_age_live_sec: int = 180
    edge_cents: float = 5.0
    trade_size_usd: float = 10.0
    take_profit_pct: float = 15.0
    stop_loss_pct: float = 0.0
    include_fees: bool = True
    min_price: float = 0.05
    max_price: float = 0.95
    max_spread: float = 0.06
    allow_live_entries: bool = True
    # --- volume gate ---
    min_game_volume: float = 50000.0
    min_game_volume_24h: float = 5000.0
    volume_top_pct: float = 50.0
    # --- contrarian ---
    contrarian_enabled: bool = True
    book_window_dollars: float = 0.03
    lopsided_share: float = 0.70
    book_max_cents: float = 1.5
    flow_max_cents: float = 2.5
    flow_window_hours: float = 6.0
    min_flow_contracts: float = 1000.0
    overpay_min_cents: float = 1.0
    overpay_max_cents: float = 2.0
    drift_min_cents: float = 3.0
    drift_bonus_cents: float = 1.0
    contra_max_cents: float = 5.0
    microstructure_refresh_sec: int = 60
    # --- news ---
    news_enabled: bool = True
    news_refresh_sec: int = 1800
    news_fresh_hours: float = 12.0
    nba_key_top_n: int = 75
    nfl_key_rushers: int = 32
    nfl_key_receivers: int = 48
    nfl_skill_key_weight: float = 0.5
    key_players: list = field(default_factory=list)
    non_key_player_weight: float = 0.15
    article_weight: float = 0.25
    news_block_enabled: bool = True
    news_block_threshold: float = 0.75
    news_boost_enabled: bool = True
    news_boost_cents: float = 2.0
    # --- combine ---
    relaxed_edge_cents: float = 0.0
    strong_signal_cents: float = 3.0
    min_score_cents: float = 5.0
    # --- fees / maker entry / multi-variant sim ---
    taker_fee_rate: float = 0.07      # Kalshi: fee = ceil(rate * C * P * (1-P)) per order
    maker_fee_rate: float = 0.0175    # KXNFLGAME/KXNBAGAME are fee_type=quadratic_with_maker_fees
    maker_offset_cents: float = 1.0   # maker entry: rest a bid this far above the best bid
    maker_timeout_min: float = 30.0   # cancel an unfilled simulated maker bid after this long
    maker_cancel_at_start: bool = True   # cancel a pregame resting bid once the game starts
    odds_pace_until: str = "2026-11-04T00:00:00-08:00"   # spread the Odds API quota until this time (or month end if later)
    closing_soon_min: float = 60.0    # prioritise an odds refresh for sports with a game starting within this
    shared_dir: str = "data/shared"
    variants_dir: str = "data/variants"
    max_open_positions: int = 5
    live_balance_floor_usd: float = 50.0   # live runner: no new buy if balance would drop below this
    reentry_cooldown_min: float = 120.0
    max_daily_spend_usd: float = 50.0
    interval_live_sec: int = 30
    interval_idle_sec: int = 300
    state_file: str = "data/state.json"
    trade_log_csv: str = "data/trades.csv"
    event_log_jsonl: str = "data/events.jsonl"

    def path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else ROOT / p


# config.toml section/key -> Config attribute
_MAP = {
    ("mode", "dry_run"): "dry_run",
    ("kalshi", "base_url"): "kalshi_base_url",
    ("kalshi", "trade_base_url"): "kalshi_trade_base_url",
    ("kalshi", "series"): "series",
    ("odds", "base_url"): "odds_base_url",
}


def load_config(path: str | os.PathLike | None = None, env: dict | None = None) -> Config:
    env = os.environ if env is None else env
    cfg = Config()
    p = Path(path) if path else ROOT / "config.toml"
    if p.exists():
        data = tomllib.loads(p.read_text())
        for section, values in data.items():
            for key, val in values.items():
                attr = _MAP.get((section, key), key)
                if hasattr(cfg, attr):
                    setattr(cfg, attr, val)
    # env overrides: attribute name upper-cased (e.g. EDGE_CENTS, DRY_RUN, KALSHI_BASE_URL)
    for attr in cfg.__dataclass_fields__:
        raw = env.get(attr.upper())
        if raw is not None and raw != "":
            setattr(cfg, attr, _coerce(raw, getattr(cfg, attr)))
    return cfg
