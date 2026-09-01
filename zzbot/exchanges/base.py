"""Interfaces que separan la estrategia del exchange concreto.

El motor solo habla con estas dos abstracciones, asi que anadir otro exchange
o cambiar de paper a live no toca ni la estrategia ni el gestor de riesgo.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, List, Optional

from ..models import Market, Position, Series, Side


class MarketDataSource(ABC):
    """Solo lectura: precios y velas."""

    @abstractmethod
    def list_markets(self, quote: str) -> List[Market]:
        """Todos los mercados con esa moneda quote y sus estadisticas de 24h."""

    @abstractmethod
    def fetch_series(self, symbol: str, interval: str, limit: int) -> Series:
        """Velas historicas de un mercado."""

    @abstractmethod
    def fetch_prices(self, symbols: List[str]) -> Dict[str, float]:
        """Ultimo precio de varios simbolos en una sola llamada."""


class Broker(ABC):
    """Ejecucion: abrir y cerrar posiciones."""

    @abstractmethod
    def open_position(
        self,
        symbol: str,
        side: Side,
        qty: float,
        price: float,
        stop_price: float,
        take_profit_price: float,
    ) -> Position:
        ...

    @abstractmethod
    def close_position(self, position: Position, price: float, reason) -> "object":
        ...

    @property
    @abstractmethod
    def equity(self) -> float:
        ...


class ExchangeError(RuntimeError):
    pass


class RateLimited(ExchangeError):
    pass
