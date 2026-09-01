"""Reversion a la media: comprar exageraciones a la baja dentro de un rango.

Es la estrategia opuesta a seguir tendencia, y por eso exige lo contrario:
ADX bajo (mercado lateral) y precio muy lejos de su media. Operarla en plena
tendencia es la forma clasica de perder dinero "comprando barato".
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .. import indicators as ind
from ..models import Position, Series, Side, Signal
from .base import Strategy


class MeanReversion(Strategy):
    name = "mean_reversion"
    min_bars = 100

    @staticmethod
    def defaults() -> Dict[str, Any]:
        return {
            "bb_period": 20,
            "bb_mult": 2.0,
            "z_entry": -2.0,        # desviaciones tipicas bajo la media para comprar
            "z_entry_short": 2.0,
            "rsi_period": 14,
            "rsi_oversold": 30.0,
            "rsi_overbought": 70.0,
            "adx_period": 14,
            "adx_max": 22.0,        # si hay tendencia fuerte, no revertimos
            "atr_period": 14,
            "min_atr_pct": 0.2,
            "max_atr_pct": 8.0,
            "trend_filter_ema": 200,
        }

    def evaluate(self, series: Series) -> Optional[Signal]:
        if len(series) < self.min_bars:
            return None
        closes, highs, lows = series.closes, series.highs, series.lows
        price = closes[-1]

        z = ind.last_valid(ind.zscore(closes, self._p("bb_period")))
        rsi_v = ind.last_valid(ind.rsi(closes, self._p("rsi_period")))
        adx_v = ind.last_valid(ind.adx(highs, lows, closes, self._p("adx_period")))
        atr_v = ind.last_valid(ind.atr(highs, lows, closes, self._p("atr_period")))
        ema_t = ind.last_valid(ind.ema(closes, min(self._p("trend_filter_ema"), len(closes) - 1)))
        if None in (z, rsi_v, atr_v) or price <= 0:
            return None

        atr_pct = atr_v / price * 100.0
        if not self._p("min_atr_pct") <= atr_pct <= self._p("max_atr_pct"):
            return None
        if adx_v is not None and adx_v > self._p("adx_max"):
            return None  # hay tendencia: no es terreno de reversion

        if z <= self._p("z_entry") and rsi_v <= self._p("rsi_oversold"):
            score = 0.45
            score += min((abs(z) - abs(self._p("z_entry"))) * 0.15, 0.20)
            score += min((self._p("rsi_oversold") - rsi_v) / 100.0, 0.15)
            if ema_t is not None and price > ema_t:
                score += 0.15   # sobreventa dentro de una tendencia mayor alcista
            if adx_v is not None:
                score += min((self._p("adx_max") - adx_v) / 100.0, 0.10)
            return Signal(
                symbol=series.symbol,
                side=Side.LONG,
                score=round(min(score, 1.0), 4),
                price=price,
                atr=atr_v,
                reason=f"sobreventa z={z:.2f}, rsi={rsi_v:.0f}, adx={adx_v or 0:.0f}",
                meta={"z": z, "rsi": rsi_v, "adx": adx_v or 0.0, "atr_pct": atr_pct},
            )

        if self.allow_short and z >= self._p("z_entry_short") and rsi_v >= self._p("rsi_overbought"):
            score = 0.45
            score += min((z - self._p("z_entry_short")) * 0.15, 0.20)
            score += min((rsi_v - self._p("rsi_overbought")) / 100.0, 0.15)
            if ema_t is not None and price < ema_t:
                score += 0.15
            return Signal(
                symbol=series.symbol,
                side=Side.SHORT,
                score=round(min(score, 1.0), 4),
                price=price,
                atr=atr_v,
                reason=f"sobrecompra z={z:.2f}, rsi={rsi_v:.0f}",
                meta={"z": z, "rsi": rsi_v, "adx": adx_v or 0.0, "atr_pct": atr_pct},
            )
        return None

    def should_exit(self, position: Position, series: Series) -> bool:
        """El objetivo era volver a la media: al tocarla, la tesis se cumplio."""
        z = ind.last_valid(ind.zscore(series.closes, self._p("bb_period")))
        if z is None:
            return False
        return z >= 0.0 if position.side is Side.LONG else z <= 0.0
