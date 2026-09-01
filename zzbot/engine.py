"""Motor de trading: el bucle que une escaner, estrategia, riesgo y ejecucion.

Orden de cada ciclo, deliberadamente asi:

  1. Refrescar precios y equity.
  2. GESTIONAR lo abierto (stops, take profit, trailing, tiempo).
  3. Comprobar limites globales y diarios.
  4. Solo entonces, buscar nuevas entradas.

Primero se protege lo que ya esta arriesgado y despues se busca mas riesgo.
Al reves, un ciclo lento podria abrir posiciones nuevas mientras una vieja se
desangra sin que nadie mire.
"""

from __future__ import annotations

import logging
import signal as os_signal
import time
from typing import Dict, List, Optional

from .config import Config
from .exchanges.base import ExchangeError, MarketDataSource
from .exchanges.binance import BinancePublic
from .models import ExitReason, Position, Series, Side, Signal, Trade
from .notify import Notifier
from .portfolio import Portfolio
from .risk import RiskManager
from .scanner import Scanner
from .storage import Store
from .strategies import build as build_strategy

log = logging.getLogger(__name__)


class TradingEngine:
    def __init__(self, cfg: Config, source: Optional[MarketDataSource] = None, store: Optional[Store] = None):
        self.cfg = cfg
        self.source = source or BinancePublic(workers=cfg.scanner.workers)
        self.store = store if store is not None else Store(cfg.db_path)
        self.strategy = build_strategy(
            cfg.strategy.name, cfg.strategy.params, allow_short=cfg.strategy.allow_short
        )
        self.scanner = Scanner(self.source, cfg.scanner)
        self.notifier = Notifier(cfg.notify)

        self.portfolio = Portfolio(cash=cfg.initial_equity, execution=cfg.execution)
        self.risk = RiskManager(
            cfg.risk, cfg.initial_equity, state=self.store.load_risk_state() if self.store else None
        )
        self._market_meta: Dict[str, object] = {}
        self._series_cache: Dict[str, Series] = {}
        self._running = False
        self._restore()

    # ------------------------------------------------------------------

    def _restore(self) -> None:
        """Recupera efectivo y posiciones abiertas de una ejecucion anterior.

        El efectivo se lee del ultimo registro guardado, no de la config: si no,
        un reinicio borraria el resultado acumulado y el bot creeria que empieza
        de cero, justo cuando los limites de riesgo mas necesitan la verdad.
        """
        if not self.store:
            return

        history = self.store.equity_history(1)
        if history:
            self.portfolio.cash = history[0]["cash"]
            log.info("efectivo restaurado del estado anterior: %.2f", self.portfolio.cash)

        restored = 0
        for raw in self.store.load_positions():
            try:
                raw = dict(raw)
                raw["side"] = Side(raw["side"])
                pos = Position(**raw)
                self.portfolio.positions[pos.symbol] = pos
                restored += 1
            except (TypeError, ValueError, KeyError) as exc:
                log.warning("no se pudo restaurar una posicion guardada: %s", exc)
        if restored:
            log.info("restauradas %s posiciones abiertas del estado anterior", restored)

    def _persist(self, equity: float) -> None:
        if not self.store:
            return
        self.store.save_risk_state(self.risk.state)
        self.store.record_equity(equity, self.portfolio.cash, len(self.portfolio.positions))

    # ------------------------------------------------------------------

    def prices_for_open(self) -> Dict[str, float]:
        symbols = list(self.portfolio.positions.keys())
        if not symbols:
            return {}
        try:
            return self.source.fetch_prices(symbols)
        except ExchangeError as exc:
            log.error("no se pudieron obtener precios de las posiciones abiertas: %s", exc)
            # Sin precios frescos no se toca nada: mejor un ciclo perdido que
            # cerrar una posicion contra un precio inventado.
            return {}

    def manage_open_positions(self, prices: Dict[str, float]) -> List[Trade]:
        """Aplica stops, take profit, trailing y salidas por tiempo o senal."""
        closed: List[Trade] = []
        for pos in list(self.portfolio.positions.values()):
            price = prices.get(pos.symbol)
            if price is None:
                continue
            self.risk.update_position(pos, price)
            reason = self.risk.check_exit(pos, price)
            if reason is None and self.strategy.should_exit(pos, self._series_for(pos.symbol)):
                reason = ExitReason.SIGNAL_FLIP
            if reason:
                closed.append(self._close(pos, price, reason))
        return closed

    def _series_for(self, symbol: str) -> Series:
        """Velas del ciclo actual. Vacia si el escaneo no las trajo."""
        return self._series_cache.get(
            symbol, Series(symbol=symbol, interval=self.cfg.scanner.interval, candles=[])
        )

    def _close(self, pos: Position, price: float, reason: ExitReason) -> Trade:
        trade = self.portfolio.close_position(pos, price, reason)
        self.risk.on_trade_closed(trade)
        if self.store:
            self.store.record_trade(trade)
            self.store.drop_position(pos.symbol)
        self.notifier.send(
            "close",
            f"CIERRE {trade.symbol} {trade.side.value} @ {trade.exit_price:.6g}\n"
            f"motivo: {reason.value}\npnl: {trade.pnl:+.4f} ({trade.pnl_pct:+.2f}%)",
        )
        return trade

    def close_all(self, prices: Dict[str, float], reason: ExitReason = ExitReason.RISK_HALT) -> List[Trade]:
        closed = []
        for pos in list(self.portfolio.positions.values()):
            price = prices.get(pos.symbol, pos.entry_price)
            closed.append(self._close(pos, price, reason))
        return closed

    # ------------------------------------------------------------------

    def try_open(self, signals: List[Signal], equity: float,
                 prices: Optional[Dict[str, float]] = None) -> List[Position]:
        opened: List[Position] = []
        for sig in signals:
            if sig.score < self.cfg.strategy.min_score:
                continue
            if sig.side is Side.SHORT and not self.cfg.strategy.allow_short:
                continue

            meta = self._market_meta.get(sig.symbol)
            qty_step = getattr(meta, "qty_step", 0.0) or 0.0
            min_notional = max(
                getattr(meta, "min_notional", 0.0) or 0.0, self.cfg.execution.min_notional
            )

            decision = self.risk.evaluate_entry(
                sig,
                equity,
                self.portfolio.open_list(),
                qty_step=qty_step,
                min_notional=min_notional,
            )
            if not decision:
                log.debug("descartada %s: %s", sig.symbol, decision.reason)
                continue

            pos = self.portfolio.open_position(
                symbol=sig.symbol,
                side=sig.side,
                qty=decision.qty,
                price=sig.price,
                stop_price=decision.stop_price,
                take_profit_price=decision.take_profit_price,
                meta={"score": sig.score, **sig.meta},
            )
            if pos is None:
                continue
            opened.append(pos)
            if self.store:
                self.store.save_position(pos)
            self.notifier.send(
                "open",
                f"APERTURA {sig.symbol} {sig.side.value} @ {pos.entry_price:.6g}\n"
                f"{sig.reason}\nstop {pos.stop_price:.6g} | tp {pos.take_profit_price:.6g}\n"
                f"riesgo {decision.risk_amount:.2f} ({self.cfg.risk.risk_per_trade_pct}% del equity)",
            )
            # El equity efectivo cambia al comprometer capital, asi que los
            # limites de la siguiente senal se evaluan con la foto actualizada.
            # Se valora con TODOS los precios conocidos, no solo el de esta
            # senal: si no, el resto de posiciones contarian a precio de entrada.
            marks = dict(prices or {})
            marks[sig.symbol] = sig.price
            equity = self.portfolio.mark_to_market(marks)
        return opened

    # ------------------------------------------------------------------

    def run_cycle(self) -> Dict[str, object]:
        """Un ciclo completo. Es la unidad que el bucle repite.

        El escaneo va primero para tener velas frescas de TODO el universo,
        incluidas las posiciones abiertas: sin ellas la estrategia no puede
        decidir si su tesis sigue siendo valida. Las decisiones de salida usan
        esas velas, pero los precios de ejecucion vienen del ticker, que es
        mas reciente que el cierre de la ultima vela.
        """
        signals: List[Signal] = []
        scanned = 0
        try:
            result = self.scanner.scan(self.strategy)
            self._series_cache = result.series
            signals, scanned = result.signals, result.scanned
        except ExchangeError as exc:
            # Sin escaneo no hay entradas nuevas, pero lo abierto se gestiona igual.
            log.error("fallo el escaneo, este ciclo solo gestiona lo abierto: %s", exc)

        prices = self.prices_for_open()
        equity = self.portfolio.mark_to_market(prices)

        # 1) Proteger lo abierto, antes de buscar mas riesgo.
        closed = self.manage_open_positions(prices)
        if closed:
            equity = self.portfolio.mark_to_market(prices)

        # 2) Frenos duros.
        halt = self.risk.check_halt(equity)
        if halt:
            if self.cfg.risk.close_positions_on_halt and self.portfolio.positions:
                closed += self.close_all(prices)
                equity = self.portfolio.mark_to_market({})
            self.notifier.send("halt", f"BOT DETENIDO\n{halt}\nequity: {equity:.2f}")
            self._persist(equity)
            return {"halted": True, "reason": halt, "equity": equity, "closed": len(closed)}

        # 3) Frenos diarios: se siguen gestionando posiciones, pero no se abren nuevas.
        block = self.risk.daily_block(equity)
        if block:
            self.portfolio.tick_bars()
            self._persist(equity)
            return {"blocked": block, "equity": equity, "closed": len(closed), "opened": 0}

        # 4) Buscar entradas.
        opened = self.try_open(signals, equity, prices)

        self.portfolio.tick_bars()
        equity = self.portfolio.mark_to_market({**prices, **{p.symbol: p.entry_price for p in opened}})
        self._persist(equity)
        return {
            "equity": equity,
            "escaneados": scanned,
            "senales": len(signals),
            "opened": len(opened),
            "closed": len(closed),
            "abiertas": len(self.portfolio.positions),
        }

    # ------------------------------------------------------------------

    def run(self, max_cycles: Optional[int] = None) -> None:
        """Bucle principal. Se detiene con Ctrl+C o al saltar un limite duro."""
        self._running = True
        self._install_signal_handlers()

        self.scanner.build_universe(self.cfg.execution.quote_asset)
        self._market_meta = {m.symbol: m for m in self.scanner.universe}

        mode_label = "SIMULADO (paper)" if self.cfg.mode == "paper" else "DINERO REAL"
        log.info(
            "arrancando en modo %s | estrategia=%s | %s mercados | equity inicial %.2f",
            mode_label, self.strategy.name, len(self.scanner.universe), self.cfg.initial_equity,
        )
        log.info(
            "limites: perdida diaria %.2f%% | ganancia diaria %.2f%% | drawdown maximo %.2f%% | riesgo por operacion %.2f%%",
            self.cfg.risk.max_daily_loss_pct, self.cfg.risk.daily_profit_target_pct,
            self.cfg.risk.max_total_drawdown_pct, self.cfg.risk.risk_per_trade_pct,
        )

        cycles = 0
        while self._running:
            started = time.time()
            try:
                result = self.run_cycle()
                log.info("ciclo %s: %s", cycles + 1, result)
                if result.get("halted"):
                    log.error("el bot se detuvo: %s", result.get("reason"))
                    break
            except ExchangeError as exc:
                log.error("error del exchange, se reintenta en el proximo ciclo: %s", exc)
            except Exception as exc:  # noqa: BLE001
                log.exception("error inesperado en el ciclo: %s", exc)

            cycles += 1
            if max_cycles and cycles >= max_cycles:
                break
            elapsed = time.time() - started
            time.sleep(max(0.0, self.cfg.poll_seconds - elapsed))

        equity = self.portfolio.mark_to_market(self.prices_for_open())
        log.info("detenido. equity final %.2f | %s", equity, self.portfolio.stats(equity, self.cfg.initial_equity))

    def stop(self) -> None:
        self._running = False

    def _install_signal_handlers(self) -> None:
        def handler(signum, frame):  # noqa: ARG001
            log.info("senal recibida, cerrando ordenadamente tras el ciclo actual")
            self._running = False

        try:
            os_signal.signal(os_signal.SIGINT, handler)
            os_signal.signal(os_signal.SIGTERM, handler)
        except ValueError:
            # Fuera del hilo principal no se pueden instalar manejadores.
            pass
