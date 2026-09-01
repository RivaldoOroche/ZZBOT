"""Cartera simulada: posiciones, efectivo, comisiones y PnL realizado.

Es la contabilidad del modo paper y del backtest. En modo live este objeto
sigue llevando la cuenta local, pero el broker real es la fuente de verdad.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .config import ExecutionConfig
from .models import ExitReason, Position, Side, Trade, now_ms

log = logging.getLogger(__name__)


@dataclass
class Portfolio:
    cash: float
    execution: ExecutionConfig
    positions: Dict[str, Position] = field(default_factory=dict)
    trades: List[Trade] = field(default_factory=list)
    fees_paid: float = 0.0

    # ------------------------------------------------------------------

    def mark_to_market(self, prices: Dict[str, float]) -> float:
        """Equity = efectivo + valor actual de lo abierto.

        Para cortos el efectivo ya incluye lo ingresado al vender, asi que la
        posicion se valora como pasivo (por eso el signo negativo).
        """
        equity = self.cash
        for pos in self.positions.values():
            price = prices.get(pos.symbol, pos.entry_price)
            if pos.side is Side.LONG:
                equity += pos.qty * price
            else:
                equity -= pos.qty * price
        return equity

    @property
    def exposure(self) -> float:
        return sum(p.qty * p.entry_price for p in self.positions.values())

    def open_list(self) -> List[Position]:
        return list(self.positions.values())

    def has(self, symbol: str) -> bool:
        return symbol in self.positions

    # ------------------------------------------------------------------

    def _fill_price(self, price: float, side: Side, opening: bool) -> float:
        """Aplica deslizamiento en contra. Siempre en contra: es lo realista."""
        slip = self.execution.slippage_pct / 100.0
        buying = (side is Side.LONG and opening) or (side is Side.SHORT and not opening)
        return price * (1 + slip) if buying else price * (1 - slip)

    def _fee(self, notional: float) -> float:
        rate = (
            self.execution.taker_fee_pct
            if self.execution.order_type == "market"
            else self.execution.maker_fee_pct
        )
        return notional * rate / 100.0

    def open_position(
        self,
        symbol: str,
        side: Side,
        qty: float,
        price: float,
        stop_price: float,
        take_profit_price: float,
        ts_ms: Optional[int] = None,
        meta: Optional[Dict[str, float]] = None,
    ) -> Optional[Position]:
        ts_ms = now_ms() if ts_ms is None else ts_ms
        fill = self._fill_price(price, side, opening=True)
        notional = qty * fill
        fee = self._fee(notional)

        if side is Side.LONG:
            if notional + fee > self.cash:
                log.warning("efectivo insuficiente para %s: hacen falta %.2f, hay %.2f", symbol, notional + fee, self.cash)
                return None
            self.cash -= notional + fee
        else:
            # Corto simulado: se ingresa la venta y se debe la cantidad.
            self.cash += notional - fee

        self.fees_paid += fee
        pos = Position(
            symbol=symbol,
            side=side,
            qty=qty,
            entry_price=fill,
            stop_price=stop_price,
            take_profit_price=take_profit_price,
            opened_at=ts_ms,
            entry_fee=fee,
            meta=meta or {},
        )
        self.positions[symbol] = pos
        log.info(
            "ABRE %s %s qty=%.8f @ %.6g | stop %.6g | tp %.6g | comision %.4f",
            side.value.upper(), symbol, qty, fill, stop_price, take_profit_price, fee,
        )
        return pos

    def close_position(
        self,
        position: Position,
        price: float,
        reason: ExitReason,
        ts_ms: Optional[int] = None,
    ) -> Trade:
        ts_ms = now_ms() if ts_ms is None else ts_ms
        fill = self._fill_price(price, position.side, opening=False)
        notional = position.qty * fill
        fee = self._fee(notional)
        self.fees_paid += fee

        if position.side is Side.LONG:
            self.cash += notional - fee
            gross = (fill - position.entry_price) * position.qty
        else:
            self.cash -= notional + fee
            gross = (position.entry_price - fill) * position.qty

        total_fees = position.entry_fee + fee
        cost_basis = position.entry_price * position.qty
        pnl_pct = (gross - total_fees) / cost_basis * 100.0 if cost_basis else 0.0

        trade = Trade(
            symbol=position.symbol,
            side=position.side,
            qty=position.qty,
            entry_price=position.entry_price,
            exit_price=fill,
            opened_at=position.opened_at,
            closed_at=ts_ms,
            pnl=gross - total_fees,
            pnl_pct=pnl_pct,
            fees=total_fees,
            reason=reason,
            position_id=position.id,
        )
        self.trades.append(trade)
        self.positions.pop(position.symbol, None)
        log.info(
            "CIERRA %s @ %.6g | motivo=%s | pnl=%.4f (%.2f%%)",
            position.symbol, fill, reason.value, trade.pnl, trade.pnl_pct,
        )
        return trade

    def update_bars_held(self, ts_ms: int, interval_ms: int) -> None:
        """Recalcula cuantas velas lleva abierta cada posicion.

        Se deduce del tiempo transcurrido, no del numero de ciclos ejecutados:
        el motor puede dar muchos ciclos dentro de una misma vela, y antes
        `max_holding_bars` significaba 4 dias en backtest y 96 minutos en vivo.
        """
        if interval_ms <= 0:
            return
        for pos in self.positions.values():
            pos.bars_held = max(0, (ts_ms - pos.opened_at) // interval_ms)

    # ------------------------------------------------------------------

    def stats(self, equity: float, initial_equity: float) -> Dict[str, float]:
        wins = [t for t in self.trades if t.is_win]
        losses = [t for t in self.trades if not t.is_win]
        gross_win = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses))
        return {
            "operaciones": len(self.trades),
            "ganadoras": len(wins),
            "perdedoras": len(losses),
            "acierto_pct": round(len(wins) / len(self.trades) * 100.0, 2) if self.trades else 0.0,
            "pnl_total": round(equity - initial_equity, 4),
            "pnl_total_pct": round((equity - initial_equity) / initial_equity * 100.0, 3),
            "ganancia_media": round(gross_win / len(wins), 4) if wins else 0.0,
            "perdida_media": round(-gross_loss / len(losses), 4) if losses else 0.0,
            "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else float("inf") if gross_win else 0.0,
            "comisiones": round(self.fees_paid, 4),
            "expectativa_por_operacion": round((equity - initial_equity) / len(self.trades), 4) if self.trades else 0.0,
        }
