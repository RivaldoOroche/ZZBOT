"""Contrato de estrategia.

Una estrategia solo mira velas y devuelve una senal o None. No sabe nada de
dinero, tamano de posicion ni limites: eso es trabajo del gestor de riesgo.
Esa separacion es lo que permite cambiar de estrategia sin tocar los frenos.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from ..models import Position, Series, Signal


class Strategy(ABC):
    name = "base"
    min_bars = 60

    def __init__(self, params: Optional[Dict[str, Any]] = None, allow_short: bool = False):
        self.params = {**self.defaults(), **(params or {})}
        self.allow_short = allow_short

    @staticmethod
    def defaults() -> Dict[str, Any]:
        return {}

    @abstractmethod
    def evaluate(self, series: Series) -> Optional[Signal]:
        """Senal de entrada, o None si no hay nada que hacer en este mercado."""

    def should_exit(self, position: Position, series: Series) -> bool:
        """Salida por invalidacion de la tesis, aparte de stop y take profit.

        Por defecto no hace nada: los stops del gestor de riesgo mandan.
        """
        return False

    def _p(self, key: str):
        return self.params[key]
