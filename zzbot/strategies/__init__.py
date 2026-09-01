"""Registro de estrategias disponibles."""

from typing import Any, Dict, Optional

from .base import Strategy
from .breakout import Breakout
from .mean_reversion import MeanReversion
from .trend_momentum import TrendMomentum

REGISTRY = {
    TrendMomentum.name: TrendMomentum,
    MeanReversion.name: MeanReversion,
    Breakout.name: Breakout,
}


def build(name: str, params: Optional[Dict[str, Any]] = None, allow_short: bool = False) -> Strategy:
    if name not in REGISTRY:
        raise ValueError(f"estrategia desconocida: {name}. Disponibles: {sorted(REGISTRY)}")
    return REGISTRY[name](params=params, allow_short=allow_short)
