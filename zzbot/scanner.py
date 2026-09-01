"""Escaner multimercado: elige que vigilar y ordena las oportunidades.

Escanear 50 mercados no sirve de nada si 40 son ilíquidos. El filtro previo
(volumen, spread, tokens apalancados) importa mas que el numero de mercados.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

from .config import ScannerConfig
from .exchanges.base import MarketDataSource
from .exchanges.binance import is_leveraged_token
from .models import Market, Series, Signal
from .strategies.base import Strategy

log = logging.getLogger(__name__)


@dataclass
class ScanResult:
    signals: List[Signal]
    scanned: int
    universe: List[str]
    series: Dict[str, Series]


class Scanner:
    def __init__(self, source: MarketDataSource, cfg: ScannerConfig):
        self.source = source
        self.cfg = cfg
        self._universe: List[Market] = []

    def build_universe(self, quote: str = "USDT") -> List[Market]:
        """Selecciona los mercados a vigilar segun liquidez y spread."""
        markets = self.source.list_markets(quote)
        by_symbol = {m.symbol: m for m in markets}

        if self.cfg.include:
            selected = [by_symbol[s] for s in self.cfg.include if s in by_symbol]
            missing = [s for s in self.cfg.include if s not in by_symbol]
            if missing:
                log.warning("simbolos de la lista include no disponibles: %s", ", ".join(missing))
            self._universe = selected
            return selected

        excluded = set(self.cfg.exclude)
        candidates = [
            m
            for m in markets
            if m.symbol not in excluded
            and m.quote_volume_24h >= self.cfg.min_quote_volume_24h
            and (m.spread_pct <= self.cfg.max_spread_pct or m.spread_pct == 0.0)
            and not (self.cfg.exclude_leveraged and is_leveraged_token(m.symbol))
        ]
        candidates.sort(key=lambda m: m.quote_volume_24h, reverse=True)
        self._universe = candidates[: self.cfg.max_markets]
        log.info(
            "universo: %s mercados de %s tras filtrar (volumen 24h >= %s, spread <= %s%%)",
            len(self._universe), len(markets), f"{self.cfg.min_quote_volume_24h:,.0f}", self.cfg.max_spread_pct,
        )
        return self._universe

    @property
    def universe(self) -> List[Market]:
        return self._universe

    def scan(self, strategy: Strategy, skip: Optional[List[str]] = None) -> ScanResult:
        """Descarga velas de todo el universo y evalua la estrategia en cada uno."""
        if not self._universe:
            self.build_universe()
        skip_set = set(skip or [])
        symbols = [m.symbol for m in self._universe if m.symbol not in skip_set]

        fetch_many = getattr(self.source, "fetch_series_many", None)
        if callable(fetch_many):
            series_map = fetch_many(symbols, self.cfg.interval, self.cfg.lookback_bars)
        else:
            series_map = {}
            for sym in symbols:
                try:
                    series_map[sym] = self.source.fetch_series(sym, self.cfg.interval, self.cfg.lookback_bars)
                except Exception as exc:  # pragma: no cover - depende de la red
                    log.warning("no se pudo descargar %s: %s", sym, exc)

        signals: List[Signal] = []
        for symbol, series in series_map.items():
            try:
                sig = strategy.evaluate(series)
            except Exception as exc:
                log.warning("la estrategia fallo en %s: %s", symbol, exc)
                continue
            if sig:
                signals.append(sig)

        # Mejor score primero: el motor abrira posiciones en ese orden hasta
        # agotar los limites de riesgo.
        signals.sort(key=lambda s: s.score, reverse=True)
        return ScanResult(
            signals=signals[: self.cfg.top_n_signals] if self.cfg.top_n_signals else signals,
            scanned=len(series_map),
            universe=symbols,
            series=series_map,
        )
