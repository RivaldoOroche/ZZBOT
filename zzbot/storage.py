"""Diario de operaciones en SQLite.

Sirve para dos cosas: sobrevivir a un reinicio sin perder los limites del dia,
y poder auditar despues por que el bot hizo lo que hizo.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Dict, List, Optional

from .models import Position, Trade, now_ms
from .risk import RiskState

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    opened_at INTEGER NOT NULL,
    closed_at INTEGER NOT NULL,
    pnl REAL NOT NULL,
    pnl_pct REAL NOT NULL,
    fees REAL NOT NULL,
    reason TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_closed_at ON trades(closed_at);

CREATE TABLE IF NOT EXISTS open_positions (
    symbol TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS equity_curve (
    ts INTEGER PRIMARY KEY,
    equity REAL NOT NULL,
    cash REAL NOT NULL,
    open_positions INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # --- operaciones ---

    def record_trade(self, trade: Trade) -> None:
        self.conn.execute(
            """INSERT INTO trades
               (position_id, symbol, side, qty, entry_price, exit_price,
                opened_at, closed_at, pnl, pnl_pct, fees, reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                trade.position_id, trade.symbol, trade.side.value, trade.qty,
                trade.entry_price, trade.exit_price, trade.opened_at, trade.closed_at,
                trade.pnl, trade.pnl_pct, trade.fees, trade.reason.value,
            ),
        )
        self.conn.commit()

    def recent_trades(self, limit: int = 20) -> List[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT * FROM trades ORDER BY closed_at DESC LIMIT ?", (limit,)
        )
        return cur.fetchall()

    def trade_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) AS n FROM trades").fetchone()["n"]

    # --- posiciones abiertas ---

    def save_position(self, pos: Position) -> None:
        payload = {
            **{k: v for k, v in pos.__dict__.items() if k != "side"},
            "side": pos.side.value,
        }
        self.conn.execute(
            "INSERT OR REPLACE INTO open_positions (symbol, payload, updated_at) VALUES (?,?,?)",
            (pos.symbol, json.dumps(payload), now_ms()),
        )
        self.conn.commit()

    def drop_position(self, symbol: str) -> None:
        self.conn.execute("DELETE FROM open_positions WHERE symbol = ?", (symbol,))
        self.conn.commit()

    def load_positions(self) -> List[Dict]:
        return [json.loads(r["payload"]) for r in self.conn.execute("SELECT payload FROM open_positions")]

    # --- curva de equity ---

    def record_equity(self, equity: float, cash: float, open_count: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO equity_curve (ts, equity, cash, open_positions) VALUES (?,?,?,?)",
            (now_ms(), equity, cash, open_count),
        )
        self.conn.commit()

    def equity_history(self, limit: int = 500) -> List[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM equity_curve ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()

    # --- estado de riesgo ---

    def save_risk_state(self, state: RiskState) -> None:
        self.set("risk_state", json.dumps(state.to_dict()))

    def load_risk_state(self) -> Optional[RiskState]:
        raw = self.get("risk_state")
        if not raw:
            return None
        try:
            return RiskState(**json.loads(raw))
        except (TypeError, ValueError) as exc:
            log.warning("estado de riesgo guardado ilegible, se reinicia: %s", exc)
            return None

    def set(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)", (key, value))
        self.conn.commit()

    def get(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None
