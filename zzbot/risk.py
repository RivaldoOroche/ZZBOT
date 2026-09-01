"""Gestor de riesgo: decide cuanto se arriesga y cuando hay que parar.

Este modulo es el que convierte un generador de senales en algo que se puede
dejar corriendo. Tiene tres capas de frenos, de mas fina a mas gruesa:

  1. Por operacion  -> stop loss, take profit, trailing, salida por tiempo.
  2. Por dia        -> perdida maxima diaria y objetivo de ganancia diaria.
  3. Global         -> drawdown maximo desde el pico (kill switch) y objetivo total.

Ninguna estrategia puede saltarselos: el motor pregunta aqui antes de abrir nada.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .config import RiskConfig
from .models import ExitReason, Position, Side, Signal, Trade, now_ms, utc_day

log = logging.getLogger(__name__)


@dataclass
class Decision:
    """Respuesta del gestor de riesgo a una peticion de abrir posicion."""

    allowed: bool
    reason: str = ""
    qty: float = 0.0
    stop_price: float = 0.0
    take_profit_price: float = 0.0
    risk_amount: float = 0.0
    notional: float = 0.0

    def __bool__(self) -> bool:
        return self.allowed


@dataclass
class RiskState:
    """Todo lo que el gestor necesita recordar entre ciclos."""

    day: str = ""
    day_start_equity: float = 0.0
    peak_equity: float = 0.0
    realized_pnl_today: float = 0.0
    trades_today: int = 0
    consecutive_losses: int = 0
    paused_until_ms: int = 0
    pause_reason: str = ""
    day_blocked_reason: str = ""
    halted: bool = False
    halt_reason: str = ""

    def to_dict(self) -> Dict[str, object]:
        return self.__dict__.copy()


class RiskManager:
    def __init__(self, cfg: RiskConfig, initial_equity: float, state: Optional[RiskState] = None):
        self.cfg = cfg
        self.initial_equity = initial_equity
        self.state = state or RiskState()
        if not self.state.day:
            self._roll_day(initial_equity, now_ms())
        if not self.state.peak_equity:
            self.state.peak_equity = initial_equity

    # ------------------------------------------------------------------
    # Ciclo de vida diario y global
    # ------------------------------------------------------------------

    def _roll_day(self, equity: float, ts_ms: int) -> None:
        self.state.day = utc_day(ts_ms)
        self.state.day_start_equity = equity
        self.state.realized_pnl_today = 0.0
        self.state.trades_today = 0
        self.state.consecutive_losses = 0
        self.state.paused_until_ms = 0
        self.state.pause_reason = ""
        self.state.day_blocked_reason = ""

    def sync(self, equity: float, ts_ms: Optional[int] = None) -> None:
        """Actualiza dia en curso y pico de equity. Llamar al inicio de cada ciclo."""
        ts_ms = ts_ms or now_ms()
        today = utc_day(ts_ms)
        if today != self.state.day:
            log.info("nuevo dia UTC %s: se reinician los limites diarios", today)
            self._roll_day(equity, ts_ms)
        if equity > self.state.peak_equity:
            self.state.peak_equity = equity

    # --- metricas expuestas ---

    def daily_pnl_pct(self, equity: float) -> float:
        base = self.state.day_start_equity or self.initial_equity
        return (equity - base) / base * 100.0 if base else 0.0

    def drawdown_pct(self, equity: float) -> float:
        peak = self.state.peak_equity or self.initial_equity
        return (peak - equity) / peak * 100.0 if peak else 0.0

    def total_pnl_pct(self, equity: float) -> float:
        return (equity - self.initial_equity) / self.initial_equity * 100.0

    # ------------------------------------------------------------------
    # Frenos globales (kill switch)
    # ------------------------------------------------------------------

    def check_halt(self, equity: float, ts_ms: Optional[int] = None) -> Optional[str]:
        """Comprueba los limites duros. Devuelve el motivo si hay que apagar.

        Un halt no se levanta solo: exige revisar que paso y reiniciar el bot.
        """
        ts_ms = ts_ms or now_ms()
        self.sync(equity, ts_ms)
        if self.state.halted:
            return self.state.halt_reason

        dd = self.drawdown_pct(equity)
        if dd >= self.cfg.max_total_drawdown_pct:
            return self._halt(
                f"drawdown maximo alcanzado: {dd:.2f}% >= {self.cfg.max_total_drawdown_pct:.2f}% "
                f"(pico {self.state.peak_equity:.2f}, ahora {equity:.2f})"
            )

        if self.cfg.total_profit_target_pct > 0:
            total = self.total_pnl_pct(equity)
            if total >= self.cfg.total_profit_target_pct:
                return self._halt(
                    f"objetivo total de ganancia alcanzado: +{total:.2f}% >= "
                    f"{self.cfg.total_profit_target_pct:.2f}%"
                )
        return None

    def _halt(self, reason: str) -> str:
        self.state.halted = True
        self.state.halt_reason = reason
        log.warning("KILL SWITCH: %s", reason)
        return reason

    def force_halt(self, reason: str) -> str:
        return self._halt(reason)

    # ------------------------------------------------------------------
    # Frenos diarios
    # ------------------------------------------------------------------

    def daily_block(self, equity: float, ts_ms: Optional[int] = None) -> Optional[str]:
        """Motivo por el que hoy no se abren mas posiciones, o None."""
        ts_ms = ts_ms or now_ms()
        self.sync(equity, ts_ms)

        # Un limite diario, una vez tocado, manda el resto del dia: no se
        # reevalua ni se levanta con un rebote del equity.
        if self.state.day_blocked_reason:
            return self.state.day_blocked_reason

        if self.state.paused_until_ms and ts_ms < self.state.paused_until_ms:
            mins = (self.state.paused_until_ms - ts_ms) / 60000.0
            return f"{self.state.pause_reason} (pausa {mins:.0f} min restantes)"

        pnl_pct = self.daily_pnl_pct(equity)
        if pnl_pct <= -abs(self.cfg.max_daily_loss_pct):
            return self._block_day(
                f"limite de perdida diaria alcanzado: {pnl_pct:.2f}% <= -{self.cfg.max_daily_loss_pct:.2f}%"
            )
        if self.cfg.daily_profit_target_pct > 0 and pnl_pct >= self.cfg.daily_profit_target_pct:
            return self._block_day(
                f"objetivo de ganancia diaria alcanzado: +{pnl_pct:.2f}% >= "
                f"{self.cfg.daily_profit_target_pct:.2f}%"
            )
        if self.state.trades_today >= self.cfg.max_daily_trades:
            return self._block_day(
                f"limite de operaciones diarias alcanzado ({self.cfg.max_daily_trades})"
            )
        if self.state.consecutive_losses >= self.cfg.max_consecutive_losses:
            reason = f"racha de {self.state.consecutive_losses} perdidas seguidas"
            # La racha se reinicia al entrar en la pausa: el enfriamiento ES la
            # respuesta. Si no, el contador seguiria alto y el bot no volveria
            # a operar en todo el dia por una racha ya castigada.
            self.state.consecutive_losses = 0
            self._pause(ts_ms, reason)
            return self.state.pause_reason
        return None

    def _block_day(self, reason: str) -> str:
        """Cierra el dia para nuevas entradas. Se levanta solo al cambiar de dia UTC."""
        if self.state.day_blocked_reason != reason:
            log.warning("sin nuevas entradas hoy: %s", reason)
        self.state.day_blocked_reason = reason
        return reason

    def _pause(self, ts_ms: int, reason: str) -> None:
        if self.state.pause_reason != reason or ts_ms >= self.state.paused_until_ms:
            log.warning("pausa de trading (%s min): %s", self.cfg.cooldown_minutes_after_stop, reason)
        self.state.pause_reason = reason
        self.state.paused_until_ms = ts_ms + self.cfg.cooldown_minutes_after_stop * 60_000

    # ------------------------------------------------------------------
    # Dimensionamiento de posicion
    # ------------------------------------------------------------------

    def evaluate_entry(
        self,
        signal: Signal,
        equity: float,
        open_positions: List[Position],
        ts_ms: Optional[int] = None,
        qty_step: float = 0.0,
        min_notional: float = 0.0,
    ) -> Decision:
        """Aprueba o rechaza una senal, y si la aprueba calcula tamano y niveles."""
        ts_ms = ts_ms or now_ms()

        halt = self.check_halt(equity, ts_ms)
        if halt:
            return Decision(False, f"bot detenido: {halt}")
        block = self.daily_block(equity, ts_ms)
        if block:
            return Decision(False, block)

        if len(open_positions) >= self.cfg.max_open_positions:
            return Decision(False, f"maximo de posiciones abiertas ({self.cfg.max_open_positions})")

        same_symbol = [p for p in open_positions if p.symbol == signal.symbol]
        if len(same_symbol) >= self.cfg.max_positions_per_symbol:
            return Decision(False, f"ya hay posicion abierta en {signal.symbol}")

        stop_price, take_profit_price = self.compute_levels(signal)
        stop_distance = abs(signal.price - stop_price)
        if stop_distance <= 0:
            return Decision(False, "distancia al stop invalida")

        # El tamano sale del riesgo, no al reves: arriesgamos un % fijo del equity
        # y de ahi deducimos cuantas unidades caben.
        risk_budget = equity * (self.cfg.risk_per_trade_pct / 100.0)
        qty = risk_budget / stop_distance
        notional = qty * signal.price

        # Tope por posicion.
        max_notional = equity * (self.cfg.max_position_pct / 100.0)
        if notional > max_notional:
            qty = max_notional / signal.price
            notional = qty * signal.price

        # Tope de exposicion agregada.
        current_exposure = sum(p.qty * p.entry_price for p in open_positions)
        max_exposure = equity * (self.cfg.max_exposure_pct / 100.0)
        room = max_exposure - current_exposure
        if room <= 0:
            return Decision(False, f"exposicion maxima alcanzada ({self.cfg.max_exposure_pct:.0f}% del equity)")
        if notional > room:
            qty = room / signal.price
            notional = qty * signal.price

        qty = _round_step(qty, qty_step)
        notional = qty * signal.price
        floor_notional = max(min_notional, 0.0)
        if qty <= 0 or notional < floor_notional:
            return Decision(
                False,
                f"tamano por debajo del minimo operable ({notional:.2f} < {floor_notional:.2f})",
            )
        if notional > equity:
            return Decision(False, "capital insuficiente para esta posicion")

        return Decision(
            allowed=True,
            qty=qty,
            stop_price=stop_price,
            take_profit_price=take_profit_price,
            risk_amount=qty * stop_distance,
            notional=notional,
        )

    def compute_levels(self, signal: Signal) -> Tuple[float, float]:
        """Calcula stop y take profit.

        Con ATR los niveles se adaptan a la volatilidad real del mercado, pero los
        porcentajes de la config siguen actuando como tope: nunca se arriesga mas
        de stop_loss_pct por muy volatil que este el activo.
        """
        price = signal.price
        pct_stop = price * (self.cfg.stop_loss_pct / 100.0)
        pct_tp = price * (self.cfg.take_profit_pct / 100.0)

        stop_dist, tp_dist = pct_stop, pct_tp
        if self.cfg.use_atr_stops and signal.atr:
            atr_stop = signal.atr * self.cfg.atr_stop_mult
            atr_tp = signal.atr * self.cfg.atr_take_profit_mult
            # El limite porcentual manda: es el numero que el usuario configuro
            # como perdida maxima aceptable por operacion.
            stop_dist = min(atr_stop, pct_stop)
            tp_dist = min(atr_tp, pct_tp)
            # Mantener el ratio beneficio/riesgo que implica la config.
            ratio = self.cfg.take_profit_pct / self.cfg.stop_loss_pct
            tp_dist = max(tp_dist, stop_dist * ratio)

        if signal.side is Side.LONG:
            return price - stop_dist, price + tp_dist
        return price + stop_dist, price - tp_dist

    # ------------------------------------------------------------------
    # Gestion de posiciones abiertas
    # ------------------------------------------------------------------

    def update_position(self, position: Position, price: float) -> None:
        """Mueve stops dinamicos (trailing y breakeven). Nunca los afloja."""
        position.highest_price = max(position.highest_price, price)
        position.lowest_price = min(position.lowest_price, price)

        gain_pct = position.unrealized_pct(price)

        if self.cfg.breakeven_at_pct > 0 and not position.breakeven_armed:
            if gain_pct >= self.cfg.breakeven_at_pct:
                position.stop_price = (
                    max(position.stop_price, position.entry_price)
                    if position.side is Side.LONG
                    else min(position.stop_price, position.entry_price)
                )
                position.breakeven_armed = True

        if self.cfg.trailing_stop_pct > 0 and gain_pct > 0:
            dist = self.cfg.trailing_stop_pct / 100.0
            if position.side is Side.LONG:
                candidate = position.highest_price * (1 - dist)
                position.stop_price = max(position.stop_price, candidate)
            else:
                candidate = position.lowest_price * (1 + dist)
                position.stop_price = min(position.stop_price, candidate)

    def check_exit(
        self,
        position: Position,
        price: float,
        high: Optional[float] = None,
        low: Optional[float] = None,
    ) -> Optional[ExitReason]:
        """Decide si una posicion debe cerrarse ya.

        `high`/`low` permiten evaluar la vela completa en backtest. Cuando ambos
        niveles se tocan en la misma vela asumimos el stop, que es el escenario
        pesimista: preferimos subestimar resultados antes que enganarnos.
        """
        high = price if high is None else high
        low = price if low is None else low

        if position.side is Side.LONG:
            hit_stop = low <= position.stop_price
            hit_tp = high >= position.take_profit_price
        else:
            hit_stop = high >= position.stop_price
            hit_tp = low <= position.take_profit_price

        if hit_stop:
            armed = position.breakeven_armed or self.cfg.trailing_stop_pct > 0
            return ExitReason.TRAILING_STOP if armed else ExitReason.STOP_LOSS
        if hit_tp:
            return ExitReason.TAKE_PROFIT
        if self.cfg.max_holding_bars > 0 and position.bars_held >= self.cfg.max_holding_bars:
            return ExitReason.TIME_STOP
        return None

    # ------------------------------------------------------------------
    # Contabilidad
    # ------------------------------------------------------------------

    def on_trade_closed(self, trade: Trade) -> None:
        self.state.trades_today += 1
        self.state.realized_pnl_today += trade.pnl
        if trade.pnl < 0:
            self.state.consecutive_losses += 1
        elif trade.pnl > 0:
            self.state.consecutive_losses = 0

    def status(self, equity: float) -> Dict[str, object]:
        return {
            "equity": round(equity, 2),
            "dia": self.state.day,
            "pnl_dia_pct": round(self.daily_pnl_pct(equity), 3),
            "limite_perdida_diaria_pct": self.cfg.max_daily_loss_pct,
            "objetivo_ganancia_diaria_pct": self.cfg.daily_profit_target_pct,
            "pnl_total_pct": round(self.total_pnl_pct(equity), 3),
            "drawdown_pct": round(self.drawdown_pct(equity), 3),
            "drawdown_maximo_pct": self.cfg.max_total_drawdown_pct,
            "pico_equity": round(self.state.peak_equity, 2),
            "operaciones_hoy": self.state.trades_today,
            "perdidas_seguidas": self.state.consecutive_losses,
            "detenido": self.state.halted,
            "motivo_detencion": self.state.halt_reason,
            "pausa": self.state.pause_reason if self.state.paused_until_ms else "",
            "bloqueo_del_dia": self.state.day_blocked_reason,
        }


def _round_step(qty: float, step: float) -> float:
    """Ajusta la cantidad al lote del exchange, siempre hacia abajo."""
    if step and step > 0:
        return math.floor(qty / step) * step
    # Sin lote conocido, 8 decimales cubre cualquier par spot de cripto.
    return math.floor(qty * 1e8) / 1e8
