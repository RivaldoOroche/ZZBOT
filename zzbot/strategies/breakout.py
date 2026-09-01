"""Rupturas de rango (estilo Donchian) con filtro de compresion previa.

Las rupturas fiables suelen venir tras un periodo de baja volatilidad. Sin ese
filtro, la mayoria de "rupturas" son ruido dentro de un rango ya ancho.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .. import indicators as ind
from ..models import Position, Series, Side, Signal
from .base import Strategy


class Breakout(Strategy):
    name = "breakout"
    min_bars = 120

    @staticmethod
    def defaults() -> Dict[str, Any]:
        return {
            "channel_bars": 40,
            "squeeze_lookback": 60,
            "squeeze_ratio": 0.85,   # ATR actual vs ATR medio: menor = mercado comprimido
            "atr_period": 14,
            "min_atr_pct": 0.15,
            "max_atr_pct": 7.0,
            "volume_factor": 1.3,    # una ruptura sin volumen rara vez aguanta
            "confirm_close": True,
        }

    def evaluate(self, series: Series) -> Optional[Signal]:
        n = self._p("channel_bars")
        if len(series) < max(self.min_bars, n + 5):
            return None
        closes, highs, lows, vols = series.closes, series.highs, series.lows, series.volumes
        price = closes[-1]

        # El canal se mide con las velas ANTERIORES a la actual: incluir la vela
        # que rompe haria que el maximo fuera siempre ella misma.
        window_high = max(highs[-n - 1 : -1])
        window_low = min(lows[-n - 1 : -1])
        atr_v = ind.last_valid(ind.atr(highs, lows, closes, self._p("atr_period")))
        if atr_v is None or price <= 0:
            return None
        atr_pct = atr_v / price * 100.0
        if not self._p("min_atr_pct") <= atr_pct <= self._p("max_atr_pct"):
            return None

        atr_series = [v for v in ind.atr(highs, lows, closes, self._p("atr_period")) if v is not None]
        lookback = self._p("squeeze_lookback")
        if len(atr_series) < lookback:
            return None
        atr_mean = sum(atr_series[-lookback:]) / lookback
        squeezed = atr_mean > 0 and (atr_v / atr_mean) <= self._p("squeeze_ratio")

        vol_ma = ind.last_valid(ind.sma(vols, 20)) or 0.0
        vol_ok = vol_ma > 0 and vols[-1] >= vol_ma * self._p("volume_factor")

        ref = closes[-1] if self._p("confirm_close") else highs[-1]
        if ref > window_high:
            score = 0.45
            score += min((ref - window_high) / max(atr_v, 1e-9) * 0.20, 0.20)
            if squeezed:
                score += 0.20
            if vol_ok:
                score += 0.15
            return Signal(
                symbol=series.symbol,
                side=Side.LONG,
                score=round(min(score, 1.0), 4),
                price=price,
                atr=atr_v,
                reason=f"ruptura de maximo de {n} velas ({window_high:.6g})",
                meta={"channel_high": window_high, "atr_pct": atr_pct, "squeeze": float(squeezed)},
            )

        ref_low = closes[-1] if self._p("confirm_close") else lows[-1]
        if self.allow_short and ref_low < window_low:
            score = 0.45
            score += min((window_low - ref_low) / max(atr_v, 1e-9) * 0.20, 0.20)
            if squeezed:
                score += 0.20
            if vol_ok:
                score += 0.15
            return Signal(
                symbol=series.symbol,
                side=Side.SHORT,
                score=round(min(score, 1.0), 4),
                price=price,
                atr=atr_v,
                reason=f"ruptura de minimo de {n} velas ({window_low:.6g})",
                meta={"channel_low": window_low, "atr_pct": atr_pct, "squeeze": float(squeezed)},
            )
        return None
