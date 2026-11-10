"""Kalshi Trade API v2 client: public market data + RSA-PSS signed trading calls.

Docs: https://docs.kalshi.com  (auth: getting_started/quick_start_authenticated_requests,
orders: api-reference/orders/create-order-v2 -> POST /portfolio/events/orders)
"""
from __future__ import annotations

import base64
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlparse

import requests

from .teams import SERIES_TO_SPORT, canonical_abbr


class KalshiError(RuntimeError):
    pass


def _dollars(m: dict, name: str) -> float | None:
    """Read a price as dollars. New API: '<name>_dollars' strings; legacy: integer cents."""
    v = m.get(f"{name}_dollars")
    if v not in (None, ""):
        return float(v)
    v = m.get(name)
    if v is None:
        return None
    return float(v) / 100.0


def _fp(m: dict, name: str) -> float | None:
    v = m.get(f"{name}_fp", m.get(name))
    return None if v in (None, "") else float(v)


def _ts(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


@dataclass
class KalshiMarket:
    ticker: str
    event_ticker: str
    series: str
    sport: str
    team_abbr: str          # team the YES side pays on
    team_label: str         # yes_sub_title, e.g. "Los Angeles L"
    event_teams: tuple      # (away_abbr, home_abbr) parsed from event ticker, best effort
    status: str
    yes_bid: float | None
    yes_ask: float | None
    yes_ask_size: float | None
    last_price: float | None
    start_time: datetime | None   # ESTIMATED scheduled start (see parse_market)
    close_time: datetime | None
    result: str
    title: str
    yes_bid_size: float | None = None
    volume: float = 0.0           # lifetime contracts traded
    volume_24h: float = 0.0
    open_interest: float = 0.0
    previous_price: float | None = None   # Kalshi 'previous_price' (~24h ago)

    @property
    def mid(self) -> float | None:
        if self.yes_bid is None or self.yes_ask is None:
            return self.last_price
        return (self.yes_bid + self.yes_ask) / 2

    @property
    def spread(self) -> float | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return self.yes_ask - self.yes_bid


# Kalshi's occurrence_datetime on game markets is the *expected expiration* (~game end), not
# kickoff/tip-off. Subtract a typical game length to estimate the start time.
GAME_LENGTH = {"americanfootball_nfl": timedelta(hours=3), "basketball_nba": timedelta(hours=2, minutes=30)}


def parse_market(m: dict, series: str) -> KalshiMarket:
    sport = SERIES_TO_SPORT.get(series, "")
    occ = _ts(m.get("occurrence_datetime") or m.get("expected_expiration_time"))
    start = occ - GAME_LENGTH.get(sport, timedelta(0)) if occ else None
    team = m["ticker"].rsplit("-", 1)[-1]
    team = canonical_abbr(sport, team) if sport else team
    return KalshiMarket(
        ticker=m["ticker"], event_ticker=m.get("event_ticker", ""), series=series, sport=sport,
        team_abbr=team, team_label=m.get("yes_sub_title") or m.get("title", ""),
        event_teams=(), status=m.get("status", ""),
        yes_bid=_dollars(m, "yes_bid"), yes_ask=_dollars(m, "yes_ask"),
        yes_ask_size=_fp(m, "yes_ask_size"), last_price=_dollars(m, "last_price"),
        start_time=start,
        close_time=_ts(m.get("close_time")), result=m.get("result", "") or "", title=m.get("title", ""),
        yes_bid_size=_fp(m, "yes_bid_size"), volume=_fp(m, "volume") or 0.0,
        volume_24h=_fp(m, "volume_24h") or 0.0, open_interest=_fp(m, "open_interest") or 0.0,
        previous_price=_dollars(m, "previous_price") or None,
    )


def group_events(markets: list[KalshiMarket]) -> dict[str, list[KalshiMarket]]:
    """event_ticker -> its team markets (normally 2). Fills event_teams on each market."""
    out: dict[str, list[KalshiMarket]] = {}
    for mk in markets:
        out.setdefault(mk.event_ticker, []).append(mk)
    for ev, mks in out.items():
        abbrs = {m.team_abbr for m in mks}
        code = ev.rsplit("-", 1)[-1][7:]  # strip YYMMMDD -> e.g. "TORLAL"
        pair = ()
        for a in abbrs:
            if code.startswith(a) and code[len(a):] in abbrs:
                pair = (a, code[len(a):])
                break
        for m in mks:
            m.event_teams = pair or tuple(sorted(abbrs))
    return out


class KalshiClient:
    def __init__(self, base_url: str, trade_base_url: str | None = None,
                 key_id: str | None = None, private_key_pem: bytes | None = None, timeout: float = 15):
        self.base_url = base_url.rstrip("/")
        self.trade_base_url = (trade_base_url or base_url).rstrip("/")
        self.key_id = key_id
        self._pkey = None
        if private_key_pem:
            from cryptography.hazmat.primitives import serialization
            self._pkey = serialization.load_pem_private_key(private_key_pem, password=None)
        self.s = requests.Session()
        self.timeout = timeout

    # ---------- public market data (no auth) ----------
    def _get_public(self, path: str, params: dict | None = None) -> dict:
        for attempt in range(4):
            try:
                r = self.s.get(self.base_url + path, params=params, timeout=self.timeout)
            except (requests.Timeout, requests.ConnectionError) as e:
                if attempt == 3:
                    raise KalshiError(f"GET {path} network error: {type(e).__name__}") from e
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code != 200:
                raise KalshiError(f"GET {path} -> {r.status_code}: {r.text[:200]}")
            return r.json()
        raise KalshiError(f"GET {path} rate limited")

    def open_markets(self, series: str) -> list[KalshiMarket]:
        out, cursor = [], None
        while True:
            params = {"series_ticker": series, "status": "open", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            d = self._get_public("/markets", params)
            ms = d.get("markets", [])
            out += [parse_market(m, series) for m in ms]
            cursor = d.get("cursor")
            if not cursor or not ms:
                return out

    def market(self, ticker: str) -> KalshiMarket:
        m = self._get_public(f"/markets/{ticker}")["market"]
        series = ticker.split("-", 1)[0]
        return parse_market(m, series)

    def orderbook(self, ticker: str, depth: int = 10) -> dict:
        """{'yes': [(price, size)...], 'no': [...]} resting BIDS, ascending price (best last)."""
        d = self._get_public(f"/markets/{ticker}/orderbook", {"depth": depth})
        ob = d.get("orderbook_fp") or {}
        out = {}
        for side in ("yes", "no"):
            lv = ob.get(f"{side}_dollars")
            if lv is None and d.get("orderbook"):   # legacy cents format
                lv = [[p / 100, q] for p, q in (d["orderbook"].get(side) or [])]
            out[side] = sorted((float(p), float(q)) for p, q in (lv or []))
        return out

    def trades(self, ticker: str, since_ts: int, limit: int = 1000) -> list[dict]:
        """Public trades since a unix timestamp: [{'side': 'yes'|'no', 'count': float, 'yes_price': float, 'ts': str}]"""
        d = self._get_public("/markets/trades", {"ticker": ticker, "min_ts": since_ts, "limit": limit})
        out = []
        for t in d.get("trades", []):
            side = t.get("taker_outcome_side") or t.get("taker_side")
            out.append({"side": side, "count": float(t.get("count_fp", t.get("count", 0)) or 0),
                        "yes_price": float(t.get("yes_price_dollars", 0) or 0), "ts": t.get("created_time")})
        return out

    # ---------- authenticated ----------
    @property
    def can_trade(self) -> bool:
        return bool(self.key_id and self._pkey)

    def _sign(self, ts: str, method: str, path: str) -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        msg = f"{ts}{method}{path.split('?')[0]}".encode()
        if isinstance(self._pkey, Ed25519PrivateKey):
            sig = self._pkey.sign(msg)
        else:
            sig = self._pkey.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                                   salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
        return base64.b64encode(sig).decode()

    def _signed(self, method: str, path: str, json_body: dict | None = None, params: dict | None = None) -> dict:
        """Signed request. GETs are retried on network errors / 5xx / 429 (re-signed each attempt).
        POST/DELETE are single-shot: an order POST that times out may still have reached the exchange, so the
        caller must reconcile via client_order_id instead of re-sending."""
        if not self.can_trade:
            raise KalshiError("Kalshi credentials not loaded")
        url = self.trade_base_url + path
        attempts = 3 if method == "GET" else 1
        for attempt in range(attempts):
            ts = str(int(time.time() * 1000))
            headers = {
                "KALSHI-ACCESS-KEY": self.key_id,
                "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": self._sign(ts, method, urlparse(url).path),
                "Content-Type": "application/json",
            }
            try:
                r = self.s.request(method, url, headers=headers, json=json_body, params=params, timeout=self.timeout)
            except (requests.Timeout, requests.ConnectionError) as e:
                if attempt + 1 < attempts:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise KalshiError(f"{method} {path} network error: {type(e).__name__}") from e
            if (r.status_code == 429 or r.status_code >= 500) and attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code not in (200, 201):
                err = KalshiError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
                err.status = r.status_code
                raise err
            return r.json() if r.text else {}
        raise KalshiError(f"{method} {path} failed")

    def balance(self) -> dict:
        return self._signed("GET", "/portfolio/balance")

    def balance_dollars(self) -> float:
        return float(self.balance().get("balance", 0)) / 100.0

    def positions(self, prefixes: tuple = ("KXNFLGAME-", "KXNBAGAME-")) -> dict[str, float]:
        """Non-zero market positions (YES>0, NO<0) whose ticker starts with one of `prefixes`."""
        out, cursor = {}, None
        for _ in range(20):
            params = {"count_filter": "position", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            d = self._signed("GET", "/portfolio/positions", params=params)
            for p in d.get("market_positions", []) or []:
                t = p.get("ticker", "")
                q = float(p.get("position_fp", p.get("position", 0)) or 0)
                if q != 0 and t.startswith(tuple(prefixes)):
                    out[t] = q
            cursor = d.get("cursor")
            if not cursor or not d.get("market_positions"):
                break
        return out

    def orders_for_ticker(self, ticker: str) -> list[dict]:
        d = self._signed("GET", "/portfolio/orders", params={"ticker": ticker, "limit": 200})
        return d.get("orders", []) or []

    def find_order_by_client_id(self, ticker: str, client_order_id: str) -> dict | None:
        for o in self.orders_for_ticker(ticker):
            if o.get("client_order_id") == client_order_id:
                return o
        return None

    def get_order(self, order_id: str) -> dict | None:
        return self._signed("GET", f"/portfolio/orders/{order_id}").get("order")

    def fills_for_order(self, order_id: str) -> dict:
        """Sum fills of one order: {'count', 'yes_value' (sum count*yes_price), 'fees', 'n'}."""
        d = self._signed("GET", "/portfolio/fills", params={"order_id": order_id, "limit": 200})
        n = v = fee = 0.0
        k = 0
        for x in d.get("fills", []) or []:
            if x.get("order_id") not in (None, order_id):
                continue
            c = float(x.get("count_fp", x.get("count", 0)) or 0)
            yp = x.get("yes_price_dollars")
            yp = float(yp) if yp not in (None, "") else float(x.get("yes_price", 0) or 0) / 100.0
            n += c
            v += c * yp
            fee += float(x.get("fee_cost", 0) or 0)
            k += 1
        return {"count": n, "yes_value": round(v, 6), "fees": round(fee, 6), "n": k}

    def place_ioc(self, ticker: str, book_side: str, count: int, price: float, reduce_only: bool = False,
                  client_order_id: str | None = None) -> dict:
        """Immediate-or-cancel limit order via Create Order (V2).
        book_side 'bid' = buy YES at <= price; 'ask' = sell YES at >= price."""
        body = {
            "ticker": ticker,
            "client_order_id": client_order_id or str(uuid.uuid4()),
            "side": book_side,
            "count": f"{count:.2f}",
            "price": f"{price:.4f}",
            "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
            "reduce_only": reduce_only,
        }
        return self._signed("POST", "/portfolio/events/orders", body)


def normalize_pem(raw: str | bytes) -> bytes:
    """Rebuild a valid PEM block when newlines were collapsed to spaces or arrived as literal \\n.
    Raises ValueError (never echoing key material) if no PEM block is present."""
    if isinstance(raw, bytes):
        raw = raw.decode(errors="replace")
    raw = raw.replace("\\n", "\n").strip()
    m = re.search(r"-----BEGIN ([A-Z ]+)-----(.*?)-----END \1-----", raw, re.S)
    if not m:
        raise ValueError("private key is not a PEM block")
    label, body = m.group(1), re.sub(r"\s+", "", m.group(2))
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    return ("-----BEGIN %s-----\n%s\n-----END %s-----\n" % (label, "\n".join(lines), label)).encode()


def load_private_key_from_env() -> bytes | None:
    pem = os.environ.get("KALSHI_PRIVATE_KEY")
    if pem:
        return normalize_pem(pem)
    path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    if path:
        with open(os.path.expanduser(path), "rb") as f:
            return normalize_pem(f.read())
    return None
