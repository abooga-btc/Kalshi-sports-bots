"""Persistent bot state (positions, daily spend, quota) + CSV/JSONL trade logs."""
from __future__ import annotations

import csv
import json
import time
from datetime import datetime
from pathlib import Path

CSV_FIELDS = ["time_local", "mode", "action", "ticker", "team", "contracts", "price", "fee",
              "cash", "fair", "edge_cents", "reason", "realized_pnl"]


class Ledger:
    def __init__(self, state_path: Path, csv_path: Path, jsonl_path: Path, mode: str):
        self.state_path, self.csv_path, self.jsonl_path, self.mode = state_path, csv_path, jsonl_path, mode
        for p in (state_path, csv_path, jsonl_path):
            p.parent.mkdir(parents=True, exist_ok=True)
        self.state = {"positions": {}, "spend_by_day": {}, "realized_pnl": 0.0, "closed": [],
                      "odds_last_fetch": {}, "odds_remaining": None, "pending": {}}
        if state_path.exists():
            self.state.update(json.loads(state_path.read_text()))

    # ----- persistence -----
    def save(self):
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2, default=str))
        tmp.replace(self.state_path)

    def event(self, kind: str, **data):
        rec = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"), "mode": self.mode, "kind": kind, **data}
        with self.jsonl_path.open("a") as f:
            f.write(json.dumps(rec, default=str) + "\n")

    def _csv(self, row: dict):
        new = not self.csv_path.exists()
        with self.csv_path.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            if new:
                w.writeheader()
            w.writerow({k: row.get(k, "") for k in CSV_FIELDS})

    # ----- queries -----
    @property
    def positions(self) -> dict:
        return self.state["positions"]

    def today(self) -> str:
        return datetime.now().astimezone().strftime("%Y-%m-%d")

    def spent_today(self) -> float:
        return float(self.state["spend_by_day"].get(self.today(), 0.0))

    # ----- mutations -----
    def add_spend(self, amount: float, day: str | None = None):
        day = day or self.today()
        self.state["spend_by_day"][day] = round(float(self.state["spend_by_day"].get(day, 0.0)) + amount, 4)

    def open_position(self, ticker, team, contracts, price, fee, cost, fair, edge, event_ticker,
                      count_spend: bool = True, reason: str = "edge", **extra):
        self.positions[ticker] = {"team": team, "contracts": contracts, "entry_price": price, "entry_fee": fee,
                                  "entry_cost": cost, "fair_at_entry": fair, "event_ticker": event_ticker,
                                  "opened_at": time.time(), **extra}
        if count_spend:
            self.add_spend(cost)
        self._csv({"time_local": datetime.now().astimezone().isoformat(timespec="seconds"), "mode": self.mode,
                   "action": "BUY_YES", "ticker": ticker, "team": team, "contracts": contracts, "price": price,
                   "fee": fee, "cash": -cost, "fair": round(fair, 4), "edge_cents": round((fair - price) * 100, 1),
                   "reason": reason})
        self.save()

    def close_position(self, ticker, price, fee, proceeds, reason):
        pos = self.positions.pop(ticker)
        pnl = round(proceeds - pos["entry_cost"], 4)
        self.state["realized_pnl"] = round(self.state["realized_pnl"] + pnl, 4)
        self.state["closed"].append({**pos, "ticker": ticker, "exit_price": price, "exit_fee": fee,
                                     "proceeds": proceeds, "pnl": pnl, "reason": reason, "closed_at": time.time()})
        self._csv({"time_local": datetime.now().astimezone().isoformat(timespec="seconds"), "mode": self.mode,
                   "action": "SELL_YES" if not reason.startswith("settled") else "SETTLE", "ticker": ticker,
                   "team": pos["team"], "contracts": pos["contracts"], "price": price, "fee": fee,
                   "cash": proceeds, "reason": reason, "realized_pnl": pnl})
        self.save()
        return pnl
