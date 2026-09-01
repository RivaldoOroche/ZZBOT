"""Fuente de datos sintetica para tests: reproducible y sin red."""

from __future__ import annotations

import math
import random
from typing import Dict, List

from zzbot.exchanges.base import MarketDataSource
from zzbot.models import Candle, Market, Series


def make_candles(n=400, start=100.0, drift=0.0008, noise=0.004, seed=7, start_ms=1_700_000_000_000,
                 step_ms=300_000) -> List[Candle]:
    """Serie con tendencia y ruido controlados, deterministica por `seed`."""
    rng = random.Random(seed)
    price = start
    out: List[Candle] = []
    for i in range(n):
        o = price
        price *= 1 + drift + rng.gauss(0, noise)
        c = price
        hi = max(o, c) * (1 + abs(rng.gauss(0, noise / 2)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, noise / 2)))
        ts = start_ms + i * step_ms
        out.append(Candle(ts, o, hi, lo, c, 1000 + rng.random() * 500, ts + step_ms - 1))
    return out


class FakeSource(MarketDataSource):
    def __init__(self, symbols: Dict[str, List[Candle]]):
        self.data = symbols
        self.calls = 0

    def list_markets(self, quote: str = "USDT") -> List[Market]:
        return [
            Market(symbol=s, base=s[:-4], quote=quote, quote_volume_24h=1e9,
                   last_price=c[-1].close, spread_pct=0.01, min_notional=10.0)
            for s, c in self.data.items()
        ]

    def fetch_series(self, symbol: str, interval: str = "5m", limit: int = 300) -> Series:
        self.calls += 1
        return Series(symbol, interval, self.data[symbol][-limit:])

    def fetch_prices(self, symbols: List[str]) -> Dict[str, float]:
        return {s: self.data[s][-1].close for s in symbols if s in self.data}
