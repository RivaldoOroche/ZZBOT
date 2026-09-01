"""Datos de mercado de Binance por REST publico. Solo stdlib.

No hace falta API key para leer precios y velas: las claves solo se necesitan
para operar en real, y eso vive en el broker, no aqui.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

from ..models import Candle, Market, Series
from .base import ExchangeError, MarketDataSource, RateLimited

log = logging.getLogger(__name__)

# data-api.binance.vision es el mirror publico de solo lectura y es el que
# responde desde mas regiones, asi que va primero. Los demas son respaldo.
BASE_URLS = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api1.binance.com",
]

LEVERAGED_MARKERS = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")


class BinancePublic(MarketDataSource):
    def __init__(self, timeout: float = 10.0, max_retries: int = 4, workers: int = 8):
        self.timeout = timeout
        self.max_retries = max_retries
        self.workers = workers
        self._base_idx = 0
        self._lock = threading.Lock()
        self._exchange_info: Optional[Dict[str, dict]] = None

    # --- HTTP ---

    def _get(self, path: str, params: Optional[dict] = None) -> object:
        query = ("?" + urllib.parse.urlencode(params)) if params else ""
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries):
            with self._lock:
                base = BASE_URLS[self._base_idx % len(BASE_URLS)]
            url = base + path + query
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "zzbot/1.0"})
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")[:300]
                if exc.code in (418, 429):
                    # Limite de peticiones: rotar de host y esperar mas.
                    with self._lock:
                        self._base_idx += 1
                    last_error = RateLimited(f"{exc.code} en {path}: {body}")
                elif exc.code in (403, 451) or 500 <= exc.code < 600:
                    # 451 = region restringida en ese host concreto; otro mirror
                    # del mismo exchange suele responder igual de bien.
                    with self._lock:
                        self._base_idx += 1
                    last_error = ExchangeError(f"{exc.code} en {path}: {body}")
                else:
                    raise ExchangeError(f"HTTP {exc.code} en {path}: {body}") from exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                with self._lock:
                    self._base_idx += 1
                last_error = ExchangeError(f"fallo de red en {path}: {exc}")
            # Backoff exponencial con jitter para no sincronizar reintentos.
            sleep_for = (2 ** attempt) * 0.5 + random.random() * 0.3
            log.debug("reintento %s de %s tras %.1fs (%s)", attempt + 1, path, sleep_for, last_error)
            time.sleep(sleep_for)
        raise last_error or ExchangeError(f"no se pudo obtener {path}")

    # --- API ---

    def exchange_info(self) -> Dict[str, dict]:
        if self._exchange_info is None:
            data = self._get("/api/v3/exchangeInfo")
            info: Dict[str, dict] = {}
            for s in data.get("symbols", []):
                if s.get("status") != "TRADING":
                    continue
                filters = {f["filterType"]: f for f in s.get("filters", [])}
                info[s["symbol"]] = {
                    "base": s["baseAsset"],
                    "quote": s["quoteAsset"],
                    "price_step": float(filters.get("PRICE_FILTER", {}).get("tickSize", 0) or 0),
                    "qty_step": float(filters.get("LOT_SIZE", {}).get("stepSize", 0) or 0),
                    "min_notional": float(
                        filters.get("NOTIONAL", filters.get("MIN_NOTIONAL", {})).get("minNotional", 0) or 0
                    ),
                    "spot": s.get("isSpotTradingAllowed", True),
                }
            self._exchange_info = info
        return self._exchange_info

    def list_markets(self, quote: str = "USDT") -> List[Market]:
        info = self.exchange_info()
        tickers = self._get("/api/v3/ticker/24hr")
        books = {b["symbol"]: b for b in self._get("/api/v3/ticker/bookTicker")}
        markets: List[Market] = []
        for t in tickers:
            symbol = t["symbol"]
            meta = info.get(symbol)
            if not meta or meta["quote"] != quote or not meta["spot"]:
                continue
            last = float(t["lastPrice"] or 0)
            if last <= 0:
                continue
            spread_pct = 0.0
            book = books.get(symbol)
            if book:
                bid, ask = float(book["bidPrice"] or 0), float(book["askPrice"] or 0)
                if bid > 0 and ask > 0:
                    spread_pct = (ask - bid) / ((ask + bid) / 2) * 100.0
            markets.append(
                Market(
                    symbol=symbol,
                    base=meta["base"],
                    quote=meta["quote"],
                    quote_volume_24h=float(t["quoteVolume"] or 0),
                    last_price=last,
                    change_pct_24h=float(t["priceChangePercent"] or 0),
                    spread_pct=spread_pct,
                    price_step=meta["price_step"],
                    qty_step=meta["qty_step"],
                    min_notional=meta["min_notional"],
                )
            )
        return markets

    def fetch_series(self, symbol: str, interval: str = "5m", limit: int = 300) -> Series:
        rows = self._get(
            "/api/v3/klines",
            {"symbol": symbol, "interval": interval, "limit": min(limit, 1000)},
        )
        candles = [
            Candle(
                open_time=int(r[0]),
                open=float(r[1]),
                high=float(r[2]),
                low=float(r[3]),
                close=float(r[4]),
                volume=float(r[5]),
                close_time=int(r[6]),
            )
            for r in rows
        ]
        return Series(symbol=symbol, interval=interval, candles=candles)

    def fetch_series_many(self, symbols: List[str], interval: str, limit: int) -> Dict[str, Series]:
        """Descarga en paralelo. Un simbolo que falle no tumba el escaneo entero."""
        out: Dict[str, Series] = {}
        if not symbols:
            return out
        with ThreadPoolExecutor(max_workers=max(1, self.workers)) as pool:
            futures = {pool.submit(self.fetch_series, s, interval, limit): s for s in symbols}
            for fut, symbol in futures.items():
                try:
                    out[symbol] = fut.result()
                except ExchangeError as exc:
                    log.warning("no se pudo descargar %s: %s", symbol, exc)
        return out

    def fetch_prices(self, symbols: List[str]) -> Dict[str, float]:
        if not symbols:
            return {}
        # Un solo request para todos los simbolos que nos interesan.
        params = {"symbols": json.dumps(symbols, separators=(",", ":"))}
        data = self._get("/api/v3/ticker/price", params)
        if isinstance(data, dict):
            data = [data]
        return {d["symbol"]: float(d["price"]) for d in data}

    def fetch_historical(
        self, symbol: str, interval: str, start_ms: int, end_ms: Optional[int] = None
    ) -> Series:
        """Pagina el historico completo entre dos fechas, para el backtest."""
        candles: List[Candle] = []
        cursor = start_ms
        end_ms = end_ms or int(time.time() * 1000)
        while cursor < end_ms:
            rows = self._get(
                "/api/v3/klines",
                {"symbol": symbol, "interval": interval, "startTime": cursor, "limit": 1000},
            )
            if not rows:
                break
            for r in rows:
                if int(r[0]) >= end_ms:
                    break
                candles.append(
                    Candle(
                        open_time=int(r[0]),
                        open=float(r[1]),
                        high=float(r[2]),
                        low=float(r[3]),
                        close=float(r[4]),
                        volume=float(r[5]),
                        close_time=int(r[6]),
                    )
                )
            next_cursor = int(rows[-1][0]) + 1
            if next_cursor <= cursor:
                break
            cursor = next_cursor
            if len(rows) < 1000:
                break
            time.sleep(0.12)  # cortesia con el rate limit
        return Series(symbol=symbol, interval=interval, candles=candles)


def is_leveraged_token(symbol: str) -> bool:
    return any(symbol.endswith(m) for m in LEVERAGED_MARKERS)
