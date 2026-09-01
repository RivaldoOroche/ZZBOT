"""Backtest: reproduce el historico vela a vela con la MISMA logica que en vivo.

Reglas que hacen que el resultado no sea mentira:

  * Las decisiones se toman con datos cerrados hasta la vela i, y la ejecucion
    ocurre al cierre de esa vela. Nunca se mira una vela futura.
  * Stops y take profit se evaluan contra el maximo y minimo de cada vela. Si
    ambos se tocan en la misma vela se asume el stop (escenario pesimista).
  * Comisiones y deslizamiento se aplican siempre, en contra.

Aun asi un backtest es una aproximacion: no modela profundidad de libro,
huecos de precio ni caidas del exchange. Sirve para descartar configuraciones
malas, no para prometer ganancias.
"""

from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass, field
from typing import Dict, List

from .config import Config
from .models import Candle, ExitReason, Series, Trade, interval_to_ms, utc_day
from .portfolio import Portfolio
from .risk import RiskManager
from .strategies import build as build_strategy

log = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    initial_equity: float
    final_equity: float
    trades: List[Trade]
    equity_curve: List[float] = field(default_factory=list)
    timestamps: List[int] = field(default_factory=list)
    halted_reason: str = ""
    blocked_days: int = 0
    symbols: List[str] = field(default_factory=list)
    bars: int = 0

    # --- metricas ---

    @property
    def total_return_pct(self) -> float:
        return (self.final_equity - self.initial_equity) / self.initial_equity * 100.0

    @property
    def max_drawdown_pct(self) -> float:
        peak, worst = self.initial_equity, 0.0
        for eq in self.equity_curve:
            peak = max(peak, eq)
            worst = max(worst, (peak - eq) / peak * 100.0)
        return worst

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        return len([t for t in self.trades if t.is_win]) / len(self.trades) * 100.0

    @property
    def profit_factor(self) -> float:
        wins = sum(t.pnl for t in self.trades if t.pnl > 0)
        losses = abs(sum(t.pnl for t in self.trades if t.pnl < 0))
        if not losses:
            return float("inf") if wins else 0.0
        return wins / losses

    @property
    def sharpe(self) -> float:
        """Sharpe sobre los retornos por barra, anualizado de forma aproximada."""
        rets = []
        for i in range(1, len(self.equity_curve)):
            prev = self.equity_curve[i - 1]
            if prev:
                rets.append((self.equity_curve[i] - prev) / prev)
        if len(rets) < 2:
            return 0.0
        sd = statistics.pstdev(rets)
        if sd == 0:
            return 0.0
        return statistics.fmean(rets) / sd * math.sqrt(len(rets))

    @property
    def expectancy(self) -> float:
        if not self.trades:
            return 0.0
        return sum(t.pnl for t in self.trades) / len(self.trades)

    @property
    def worst_trade_pct(self) -> float:
        return min((t.pnl_pct for t in self.trades), default=0.0)

    @property
    def best_trade_pct(self) -> float:
        return max((t.pnl_pct for t in self.trades), default=0.0)

    def exits_by_reason(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for t in self.trades:
            out[t.reason.value] = out.get(t.reason.value, 0) + 1
        return out

    def summary(self) -> Dict[str, object]:
        return {
            "mercados": len(self.symbols),
            "barras": self.bars,
            "equity_inicial": round(self.initial_equity, 2),
            "equity_final": round(self.final_equity, 2),
            "retorno_pct": round(self.total_return_pct, 3),
            "drawdown_maximo_pct": round(self.max_drawdown_pct, 3),
            "operaciones": len(self.trades),
            "acierto_pct": round(self.win_rate, 2),
            "profit_factor": round(self.profit_factor, 3) if self.profit_factor != float("inf") else "inf",
            "sharpe": round(self.sharpe, 3),
            "expectativa_por_operacion": round(self.expectancy, 4),
            "mejor_operacion_pct": round(self.best_trade_pct, 2),
            "peor_operacion_pct": round(self.worst_trade_pct, 2),
            "salidas": self.exits_by_reason(),
            "dias_bloqueados_por_limite": self.blocked_days,
            "detenido_por": self.halted_reason,
        }


class Backtester:
    """Replay multimercado sincronizado por timestamp de vela."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.strategy = build_strategy(
            cfg.strategy.name, cfg.strategy.params, allow_short=cfg.strategy.allow_short
        )
        # En vivo la estrategia solo ve las ultimas `lookback_bars` velas, asi que
        # el backtest usa la misma ventana deslizante. Ademas evita el coste
        # cuadratico de recalcular indicadores sobre un historico que crece.
        self.window_bars = max(cfg.scanner.lookback_bars, self.strategy.min_bars + 10)

    def _window(self, series: Series, i: int) -> Series:
        start = max(0, i + 1 - self.window_bars)
        return Series(series.symbol, series.interval, series.candles[start : i + 1])

    def run(self, series_map: Dict[str, Series]) -> BacktestResult:
        series_map = {s: v for s, v in series_map.items() if len(v) > self.strategy.min_bars}
        if not series_map:
            raise ValueError(
                f"no hay suficientes velas: la estrategia {self.strategy.name} "
                f"necesita al menos {self.strategy.min_bars} por mercado"
            )

        # Eje temporal comun: la union de todos los open_time, ordenada.
        timeline = sorted({c.open_time for s in series_map.values() for c in s.candles})
        index: Dict[str, Dict[int, int]] = {
            sym: {c.open_time: i for i, c in enumerate(s.candles)} for sym, s in series_map.items()
        }

        interval_ms = interval_to_ms(next(iter(series_map.values())).interval)
        portfolio = Portfolio(cash=self.cfg.initial_equity, execution=self.cfg.execution)
        risk = RiskManager(self.cfg.risk, self.cfg.initial_equity)
        risk.state.day = utc_day(timeline[0])
        risk.state.day_start_equity = self.cfg.initial_equity

        equity_curve: List[float] = []
        timestamps: List[int] = []
        halted_reason = ""
        blocked_days: set = set()
        min_notional = self.cfg.execution.min_notional

        for ts in timeline:
            # Precio de referencia de cada mercado en esta barra.
            closes: Dict[str, float] = {}
            bars: Dict[str, Candle] = {}
            for sym, s in series_map.items():
                i = index[sym].get(ts)
                if i is None:
                    continue
                bars[sym] = s.candles[i]
                closes[sym] = s.candles[i].close

            equity = portfolio.mark_to_market(closes)
            risk.sync(equity, ts)
            portfolio.update_bars_held(ts, interval_ms)

            # 1) Gestionar posiciones abiertas contra el rango completo de la vela.
            for pos in list(portfolio.positions.values()):
                candle = bars.get(pos.symbol)
                if candle is None:
                    continue
                risk.update_position(pos, candle.close)
                reason = risk.check_exit(pos, candle.close, high=candle.high, low=candle.low)
                if reason is None:
                    i = index[pos.symbol][ts]
                    if self.strategy.should_exit(pos, self._window(series_map[pos.symbol], i)):
                        reason = ExitReason.SIGNAL_FLIP
                if reason:
                    # Un stop se ejecuta a su nivel, no al cierre de la vela.
                    exit_price = (
                        pos.stop_price if reason in (ExitReason.STOP_LOSS, ExitReason.TRAILING_STOP)
                        else pos.take_profit_price if reason is ExitReason.TAKE_PROFIT
                        else candle.close
                    )
                    trade = portfolio.close_position(pos, exit_price, reason, ts_ms=ts)
                    risk.on_trade_closed(trade)

            equity = portfolio.mark_to_market(closes)

            # 2) Frenos duros.
            halt = risk.check_halt(equity, ts)
            if halt:
                halted_reason = halt
                if self.cfg.risk.close_positions_on_halt:
                    for pos in list(portfolio.positions.values()):
                        price = closes.get(pos.symbol, pos.entry_price)
                        trade = portfolio.close_position(pos, price, ExitReason.RISK_HALT, ts_ms=ts)
                        risk.on_trade_closed(trade)
                equity_curve.append(portfolio.mark_to_market({}))
                timestamps.append(ts)
                break

            # 3) Frenos diarios.
            block = risk.daily_block(equity, ts)
            hay_cupo = len(portfolio.positions) < self.cfg.risk.max_open_positions
            if not block and hay_cupo:
                # 4) Buscar entradas con datos disponibles hasta esta vela inclusive.
                # Evaluar la estrategia sin cupo libre seria trabajo tirado: el
                # gestor de riesgo rechazaria cualquier senal resultante.
                candidates = []
                for sym, s in series_map.items():
                    if sym in portfolio.positions:
                        continue
                    i = index[sym].get(ts)
                    if i is None or i < self.strategy.min_bars:
                        continue
                    sig = self.strategy.evaluate(self._window(s, i))
                    if sig and sig.score >= self.cfg.strategy.min_score:
                        candidates.append(sig)
                candidates.sort(key=lambda x: x.score, reverse=True)

                for sig in candidates:
                    decision = risk.evaluate_entry(
                        sig, equity, portfolio.open_list(), ts_ms=ts, min_notional=min_notional
                    )
                    if not decision:
                        continue
                    portfolio.open_position(
                        symbol=sig.symbol,
                        side=sig.side,
                        qty=decision.qty,
                        price=sig.price,
                        stop_price=decision.stop_price,
                        take_profit_price=decision.take_profit_price,
                        ts_ms=ts,
                        meta={"score": sig.score},
                    )
                    equity = portfolio.mark_to_market(closes)
            elif block:
                blocked_days.add(risk.state.day)

            equity_curve.append(portfolio.mark_to_market(closes))
            timestamps.append(ts)

        final_equity = equity_curve[-1] if equity_curve else self.cfg.initial_equity
        return BacktestResult(
            initial_equity=self.cfg.initial_equity,
            final_equity=final_equity,
            trades=portfolio.trades,
            equity_curve=equity_curve,
            timestamps=timestamps,
            halted_reason=halted_reason,
            blocked_days=len(blocked_days),
            symbols=list(series_map.keys()),
            bars=len(timeline),
        )
