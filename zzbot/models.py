"""Tipos de datos compartidos por todo el bot."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Sequence


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"


class ExitReason(str, Enum):
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"
    TRAILING_STOP = "trailing_stop"
    SIGNAL_FLIP = "signal_flip"
    TIME_STOP = "time_stop"
    RISK_HALT = "risk_halt"
    MANUAL = "manual"


@dataclass(frozen=True)
class Candle:
    open_time: int          # epoch en milisegundos
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int


@dataclass
class Series:
    """Una serie de velas de un mercado, con accesos por columna."""

    symbol: str
    interval: str
    candles: List[Candle]

    @property
    def closes(self) -> List[float]:
        return [c.close for c in self.candles]

    @property
    def highs(self) -> List[float]:
        return [c.high for c in self.candles]

    @property
    def lows(self) -> List[float]:
        return [c.low for c in self.candles]

    @property
    def volumes(self) -> List[float]:
        return [c.volume for c in self.candles]

    @property
    def last_price(self) -> float:
        return self.candles[-1].close

    def __len__(self) -> int:
        return len(self.candles)


@dataclass
class Market:
    symbol: str
    base: str
    quote: str
    quote_volume_24h: float = 0.0
    last_price: float = 0.0
    change_pct_24h: float = 0.0
    spread_pct: float = 0.0
    price_step: float = 0.0
    qty_step: float = 0.0
    min_notional: float = 0.0


@dataclass
class Signal:
    """Una intencion de operar, antes de que el gestor de riesgo la apruebe."""

    symbol: str
    side: Side
    score: float                 # confianza 0..1
    price: float
    atr: Optional[float] = None
    reason: str = ""
    meta: Dict[str, float] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.symbol} {self.side.value} score={self.score:.2f} @ {self.price:.6f} ({self.reason})"


@dataclass
class Position:
    symbol: str
    side: Side
    qty: float
    entry_price: float
    stop_price: float
    take_profit_price: float
    opened_at: int
    bars_held: int = 0
    highest_price: float = 0.0     # para trailing en largos
    lowest_price: float = 0.0      # para trailing en cortos
    entry_fee: float = 0.0
    breakeven_armed: bool = False
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    meta: Dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.highest_price:
            self.highest_price = self.entry_price
        if not self.lowest_price:
            self.lowest_price = self.entry_price

    @property
    def notional(self) -> float:
        return self.qty * self.entry_price

    def unrealized_pnl(self, price: float) -> float:
        direction = 1 if self.side is Side.LONG else -1
        return (price - self.entry_price) * self.qty * direction

    def unrealized_pct(self, price: float) -> float:
        direction = 1 if self.side is Side.LONG else -1
        return direction * (price - self.entry_price) / self.entry_price * 100.0

    def risk_amount(self) -> float:
        """Cuanto se pierde si salta el stop. Es el numero que el sizing controla."""
        return abs(self.entry_price - self.stop_price) * self.qty


@dataclass
class Trade:
    """Una operacion cerrada, ya con su resultado."""

    symbol: str
    side: Side
    qty: float
    entry_price: float
    exit_price: float
    opened_at: int
    closed_at: int
    pnl: float
    pnl_pct: float
    fees: float
    reason: ExitReason
    position_id: str = ""

    @property
    def is_win(self) -> bool:
        return self.pnl > 0


def now_ms() -> int:
    return int(time.time() * 1000)


def utc_day(ts_ms: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts_ms / 1000))
