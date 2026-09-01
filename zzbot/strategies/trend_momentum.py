"""Seguimiento de tendencia con confirmacion de momentum.

Idea: entrar solo cuando varias cosas independientes apuntan al mismo lado
(estructura de medias, fuerza de tendencia, momentum y volumen). Cada condicion
suma al score; la config decide cuanto score exige para operar.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .. import indicators as ind
from ..models import Position, Series, Side, Signal
from .base import Strategy


class TrendMomentum(Strategy):
    name = "trend_momentum"
    min_bars = 120

    @staticmethod
    def defaults() -> Dict[str, Any]:
        return {
            "ema_fast": 20,
            "ema_slow": 50,
            "ema_trend": 200,
            "rsi_period": 14,
            "rsi_long_min": 52.0,
            "rsi_long_max": 78.0,   # por encima de esto ya es euforia: mal sitio para entrar
            "rsi_short_max": 48.0,
            "rsi_short_min": 22.0,
            "adx_period": 14,
            "adx_min": 20.0,        # por debajo el mercado esta lateral
            "atr_period": 14,
            "min_atr_pct": 0.15,    # sin volatilidad no hay recorrido que capturar
            "max_atr_pct": 6.0,     # con demasiada, el stop salta por ruido
            "volume_factor": 1.1,   # volumen reciente vs media
            "momentum_bars": 12,
        }

    def evaluate(self, series: Series) -> Optional[Signal]:
        if len(series) < self.min_bars:
            return None

        closes, highs, lows, vols = series.closes, series.highs, series.lows, series.volumes
        price = closes[-1]

        ema_f = ind.last_valid(ind.ema(closes, self._p("ema_fast")))
        ema_s = ind.last_valid(ind.ema(closes, self._p("ema_slow")))
        ema_t = ind.last_valid(ind.ema(closes, min(self._p("ema_trend"), len(closes) - 1)))
        rsi_v = ind.last_valid(ind.rsi(closes, self._p("rsi_period")))
        adx_v = ind.last_valid(ind.adx(highs, lows, closes, self._p("adx_period")))
        atr_v = ind.last_valid(ind.atr(highs, lows, closes, self._p("atr_period")))
        mom = ind.last_valid(ind.pct_change(closes, self._p("momentum_bars")))

        if None in (ema_f, ema_s, rsi_v, atr_v, mom) or price <= 0:
            return None

        atr_pct = atr_v / price * 100.0
        if not self._p("min_atr_pct") <= atr_pct <= self._p("max_atr_pct"):
            return None
        if adx_v is not None and adx_v < self._p("adx_min"):
            return None

        vol_ma = ind.last_valid(ind.sma(vols, 20)) or 0.0
        vol_ok = vol_ma > 0 and vols[-1] >= vol_ma * self._p("volume_factor")

        long_score = self._score_long(price, ema_f, ema_s, ema_t, rsi_v, adx_v, mom, vol_ok)
        short_score = (
            self._score_short(price, ema_f, ema_s, ema_t, rsi_v, adx_v, mom, vol_ok)
            if self.allow_short
            else 0.0
        )

        if long_score >= short_score and long_score > 0:
            return Signal(
                symbol=series.symbol,
                side=Side.LONG,
                score=round(min(long_score, 1.0), 4),
                price=price,
                atr=atr_v,
                reason=f"tendencia alcista ema{self._p('ema_fast')}>{self._p('ema_slow')}, rsi={rsi_v:.0f}, adx={adx_v or 0:.0f}",
                meta={"rsi": rsi_v, "adx": adx_v or 0.0, "atr_pct": atr_pct, "momentum": mom * 100},
            )
        if short_score > 0:
            return Signal(
                symbol=series.symbol,
                side=Side.SHORT,
                score=round(min(short_score, 1.0), 4),
                price=price,
                atr=atr_v,
                reason=f"tendencia bajista, rsi={rsi_v:.0f}, adx={adx_v or 0:.0f}",
                meta={"rsi": rsi_v, "adx": adx_v or 0.0, "atr_pct": atr_pct, "momentum": mom * 100},
            )
        return None

    def _score_long(self, price, ema_f, ema_s, ema_t, rsi_v, adx_v, mom, vol_ok) -> float:
        if not (ema_f > ema_s and price > ema_f):
            return 0.0
        if not self._p("rsi_long_min") <= rsi_v <= self._p("rsi_long_max"):
            return 0.0
        if mom <= 0:
            return 0.0
        score = 0.40
        if ema_t is not None and price > ema_t:
            score += 0.15                      # alineado con la tendencia mayor
        if adx_v is not None:
            score += min((adx_v - self._p("adx_min")) / 40.0, 0.15)
        score += min(mom * 4.0, 0.15)          # momentum reciente
        if vol_ok:
            score += 0.10
        separation = (ema_f - ema_s) / price
        score += min(separation * 20.0, 0.10)  # cuanto mas abiertas las medias, mas clara la tendencia
        return score

    def _score_short(self, price, ema_f, ema_s, ema_t, rsi_v, adx_v, mom, vol_ok) -> float:
        if not (ema_f < ema_s and price < ema_f):
            return 0.0
        if not self._p("rsi_short_min") <= rsi_v <= self._p("rsi_short_max"):
            return 0.0
        if mom >= 0:
            return 0.0
        score = 0.40
        if ema_t is not None and price < ema_t:
            score += 0.15
        if adx_v is not None:
            score += min((adx_v - self._p("adx_min")) / 40.0, 0.15)
        score += min(abs(mom) * 4.0, 0.15)
        if vol_ok:
            score += 0.10
        separation = (ema_s - ema_f) / price
        score += min(separation * 20.0, 0.10)
        return score

    def should_exit(self, position: Position, series: Series) -> bool:
        """La tesis era la tendencia: si el cruce de medias se invierte, se sale."""
        closes = series.closes
        ema_f = ind.last_valid(ind.ema(closes, self._p("ema_fast")))
        ema_s = ind.last_valid(ind.ema(closes, self._p("ema_slow")))
        if ema_f is None or ema_s is None:
            return False
        if position.side is Side.LONG:
            return ema_f < ema_s
        return ema_f > ema_s
