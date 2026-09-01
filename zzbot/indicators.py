"""Indicadores tecnicos sobre listas de floats. Sin dependencias externas.

Todas las funciones devuelven listas de la misma longitud que la entrada,
usando None en las posiciones donde el indicador aun no tiene datos suficientes.
Esto evita desalineaciones de indices al combinar varios indicadores.
"""

from __future__ import annotations

import math
from typing import List, Optional, Sequence

Num = Optional[float]


def sma(values: Sequence[float], period: int) -> List[Num]:
    if period <= 0:
        raise ValueError("period debe ser > 0")
    out: List[Num] = [None] * len(values)
    running = 0.0
    for i, v in enumerate(values):
        running += v
        if i >= period:
            running -= values[i - period]
        if i >= period - 1:
            out[i] = running / period
    return out


def ema(values: Sequence[float], period: int) -> List[Num]:
    if period <= 0:
        raise ValueError("period debe ser > 0")
    out: List[Num] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2.0 / (period + 1.0)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rma(values: Sequence[float], period: int) -> List[Num]:
    """Media movil de Wilder, la que usan RSI y ATR originales."""
    out: List[Num] = [None] * len(values)
    if len(values) < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = (prev * (period - 1) + values[i]) / period
        out[i] = prev
    return out


def rsi(closes: Sequence[float], period: int = 14) -> List[Num]:
    out: List[Num] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = [0.0] * len(closes)
    losses = [0.0] * len(closes)
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains[i] = max(delta, 0.0)
        losses[i] = max(-delta, 0.0)
    # Wilder arranca en el indice 1 (el primer delta valido).
    avg_gain = sum(gains[1 : period + 1]) / period
    avg_loss = sum(losses[1 : period + 1]) / period
    out[period] = _rsi_value(avg_gain, avg_loss)
    for i in range(period + 1, len(closes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def true_range(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> List[float]:
    tr = [highs[0] - lows[0]] if closes else []
    for i in range(1, len(closes)):
        tr.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
        )
    return tr


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14) -> List[Num]:
    """Average True Range: la unidad de volatilidad con la que dimensionamos stops."""
    if not closes:
        return []
    return rma(true_range(highs, lows, closes), period)


def stdev(values: Sequence[float], period: int) -> List[Num]:
    out: List[Num] = [None] * len(values)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1 : i + 1]
        mean = sum(window) / period
        var = sum((v - mean) ** 2 for v in window) / period
        out[i] = math.sqrt(var)
    return out


def bollinger(values: Sequence[float], period: int = 20, mult: float = 2.0):
    mid = sma(values, period)
    sd = stdev(values, period)
    upper: List[Num] = [None] * len(values)
    lower: List[Num] = [None] * len(values)
    for i in range(len(values)):
        if mid[i] is not None and sd[i] is not None:
            upper[i] = mid[i] + mult * sd[i]
            lower[i] = mid[i] - mult * sd[i]
    return mid, upper, lower


def zscore(values: Sequence[float], period: int = 20) -> List[Num]:
    mid = sma(values, period)
    sd = stdev(values, period)
    out: List[Num] = [None] * len(values)
    for i in range(len(values)):
        if mid[i] is not None and sd[i] not in (None, 0.0):
            out[i] = (values[i] - mid[i]) / sd[i]
    return out


def macd(closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9):
    ef = ema(closes, fast)
    es = ema(closes, slow)
    line: List[Num] = [None] * len(closes)
    for i in range(len(closes)):
        if ef[i] is not None and es[i] is not None:
            line[i] = ef[i] - es[i]
    valid = [v for v in line if v is not None]
    sig_valid = ema(valid, signal)
    sig: List[Num] = [None] * len(closes)
    hist: List[Num] = [None] * len(closes)
    offset = len(closes) - len(valid)
    for j, v in enumerate(sig_valid):
        i = offset + j
        sig[i] = v
        if v is not None and line[i] is not None:
            hist[i] = line[i] - v
    return line, sig, hist


def adx(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14) -> List[Num]:
    """Fuerza de tendencia. Alto = tendencia clara, bajo = rango lateral."""
    n = len(closes)
    out: List[Num] = [None] * n
    if n < period * 2 + 1:
        return out
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm[i] = up if (up > down and up > 0) else 0.0
        minus_dm[i] = down if (down > up and down > 0) else 0.0
    tr = true_range(highs, lows, closes)
    atr_s = rma(tr, period)
    plus_s = rma(plus_dm, period)
    minus_s = rma(minus_dm, period)
    dx: List[Num] = [None] * n
    for i in range(n):
        if atr_s[i] in (None, 0.0) or plus_s[i] is None or minus_s[i] is None:
            continue
        pdi = 100.0 * plus_s[i] / atr_s[i]
        mdi = 100.0 * minus_s[i] / atr_s[i]
        denom = pdi + mdi
        dx[i] = 0.0 if denom == 0 else 100.0 * abs(pdi - mdi) / denom
    valid = [v for v in dx if v is not None]
    smoothed = rma(valid, period)
    offset = n - len(valid)
    for j, v in enumerate(smoothed):
        out[offset + j] = v
    return out


def pct_change(values: Sequence[float], lookback: int) -> List[Num]:
    out: List[Num] = [None] * len(values)
    for i in range(lookback, len(values)):
        base = values[i - lookback]
        if base:
            out[i] = (values[i] - base) / base
    return out


def last_valid(series: Sequence[Num]) -> Num:
    for v in reversed(series):
        if v is not None:
            return v
    return None
