"""Tests del gestor de riesgo.

Son los tests que mas importan: si estos fallan, el bot puede perder mas de lo
que el usuario autorizo.
"""

import unittest

from zzbot.config import Config, RiskConfig
from zzbot.models import ExitReason, Position, Side, Signal, Trade, now_ms
from zzbot.risk import RiskManager


def mk_signal(price=100.0, atr=1.0, side=Side.LONG):
    return Signal(symbol="TESTUSDT", side=side, score=0.9, price=price, atr=atr)


def mk_trade(pnl):
    return Trade("TESTUSDT", Side.LONG, 1.0, 100.0, 100.0 + pnl, now_ms(), now_ms(),
                 pnl, pnl, 0.0, ExitReason.TAKE_PROFIT)


class TestSizing(unittest.TestCase):
    def test_riesgo_por_operacion_se_respeta(self):
        cfg = RiskConfig(risk_per_trade_pct=1.0, stop_loss_pct=2.0, use_atr_stops=False,
                         max_position_pct=100.0)
        rm = RiskManager(cfg, 10_000.0)
        d = rm.evaluate_entry(mk_signal(), 10_000.0, [])
        self.assertTrue(d.allowed)
        # 1% de 10.000 = 100 de riesgo, con stop a 2 de distancia -> 50 unidades.
        self.assertAlmostEqual(d.risk_amount, 100.0, places=2)
        self.assertAlmostEqual(d.qty, 50.0, places=4)

    def test_tope_de_posicion_recorta_el_tamano(self):
        cfg = RiskConfig(risk_per_trade_pct=2.0, stop_loss_pct=0.5, use_atr_stops=False,
                         max_position_pct=10.0)
        rm = RiskManager(cfg, 1_000.0)
        d = rm.evaluate_entry(mk_signal(), 1_000.0, [])
        self.assertTrue(d.allowed)
        self.assertLessEqual(d.notional, 100.0 + 1e-6)  # 10% de 1000

    def test_stop_porcentual_es_el_techo_aunque_el_atr_sea_mayor(self):
        cfg = RiskConfig(stop_loss_pct=1.0, use_atr_stops=True, atr_stop_mult=5.0)
        rm = RiskManager(cfg, 1_000.0)
        # ATR de 2 sobre precio 100 pediria un stop del 10%; el limite es 1%.
        stop, _tp = rm.compute_levels(mk_signal(price=100.0, atr=2.0))
        self.assertAlmostEqual(stop, 99.0, places=6)

    def test_ratio_beneficio_riesgo_se_mantiene(self):
        cfg = RiskConfig(stop_loss_pct=1.0, take_profit_pct=3.0, use_atr_stops=True,
                         atr_stop_mult=0.5, atr_take_profit_mult=0.5)
        rm = RiskManager(cfg, 1_000.0)
        stop, tp = rm.compute_levels(mk_signal(price=100.0, atr=1.0))
        riesgo, beneficio = 100.0 - stop, tp - 100.0
        self.assertAlmostEqual(beneficio / riesgo, 3.0, places=6)

    def test_rechaza_si_no_llega_al_minimo_operable(self):
        rm = RiskManager(RiskConfig(risk_per_trade_pct=0.01), 100.0)
        d = rm.evaluate_entry(mk_signal(), 100.0, [], min_notional=50.0)
        self.assertFalse(d.allowed)
        self.assertIn("minimo operable", d.reason)


class TestLimitesDiarios(unittest.TestCase):
    def test_perdida_diaria_bloquea_nuevas_entradas(self):
        cfg = RiskConfig(max_daily_loss_pct=3.0)
        rm = RiskManager(cfg, 1_000.0)
        self.assertIsNone(rm.daily_block(990.0))          # -1%: sigue operando
        self.assertIsNotNone(rm.daily_block(969.0))       # -3.1%: bloqueado
        d = rm.evaluate_entry(mk_signal(), 969.0, [])
        self.assertFalse(d.allowed)
        self.assertIn("perdida diaria", d.reason)

    def test_objetivo_de_ganancia_diaria_tambien_para(self):
        cfg = RiskConfig(daily_profit_target_pct=5.0)
        rm = RiskManager(cfg, 1_000.0)
        self.assertIsNone(rm.daily_block(1_040.0))
        block = rm.daily_block(1_051.0)
        self.assertIsNotNone(block)
        self.assertIn("ganancia diaria", block)

    def test_racha_de_perdidas_pausa(self):
        cfg = RiskConfig(max_consecutive_losses=3)
        rm = RiskManager(cfg, 1_000.0)
        for _ in range(3):
            rm.on_trade_closed(mk_trade(-1.0))
        self.assertIn("perdidas seguidas", rm.daily_block(997.0) or "")

    def test_una_ganancia_reinicia_la_racha(self):
        rm = RiskManager(RiskConfig(max_consecutive_losses=3), 1_000.0)
        rm.on_trade_closed(mk_trade(-1.0))
        rm.on_trade_closed(mk_trade(-1.0))
        rm.on_trade_closed(mk_trade(+2.0))
        self.assertEqual(rm.state.consecutive_losses, 0)

    def test_limite_de_operaciones_diarias(self):
        rm = RiskManager(RiskConfig(max_daily_trades=2), 1_000.0)
        rm.on_trade_closed(mk_trade(0.5))
        rm.on_trade_closed(mk_trade(0.5))
        self.assertIn("operaciones diarias", rm.daily_block(1_001.0) or "")

    def test_los_limites_se_reinician_al_cambiar_de_dia(self):
        rm = RiskManager(RiskConfig(max_daily_loss_pct=3.0), 1_000.0)
        day1 = 1_700_000_000_000
        rm.sync(1_000.0, day1)
        rm.daily_block(950.0, day1)
        self.assertTrue(rm.state.day_blocked_reason)
        rm.sync(950.0, day1 + 86_400_000 * 2)   # dos dias despues
        self.assertEqual(rm.state.day_blocked_reason, "")
        self.assertIsNone(rm.daily_block(950.0, day1 + 86_400_000 * 2))
        self.assertAlmostEqual(rm.state.day_start_equity, 950.0)

    def test_el_limite_diario_no_se_levanta_si_el_equity_rebota(self):
        """Tocado el limite, el dia esta cerrado aunque el mercado se recupere."""
        rm = RiskManager(RiskConfig(max_daily_loss_pct=3.0), 1_000.0)
        ts = now_ms()
        self.assertIsNotNone(rm.daily_block(960.0, ts))
        self.assertIsNotNone(rm.daily_block(1_020.0, ts))   # rebote: sigue cerrado

    def test_la_racha_se_reinicia_al_entrar_en_la_pausa(self):
        """El enfriamiento ES el castigo; si no, la racha bloquearia todo el dia."""
        cfg = RiskConfig(max_consecutive_losses=3, cooldown_minutes_after_stop=30)
        rm = RiskManager(cfg, 1_000.0)
        ts = now_ms()
        for _ in range(3):
            rm.on_trade_closed(mk_trade(-1.0))
        self.assertIn("perdidas seguidas", rm.daily_block(997.0, ts) or "")
        self.assertEqual(rm.state.consecutive_losses, 0)
        # Durante la pausa sigue bloqueado...
        self.assertIsNotNone(rm.daily_block(997.0, ts + 10 * 60_000))
        # ...y al expirar vuelve a operar.
        self.assertIsNone(rm.daily_block(997.0, ts + 31 * 60_000))


class TestKillSwitch(unittest.TestCase):
    def test_drawdown_maximo_detiene_el_bot(self):
        rm = RiskManager(RiskConfig(max_total_drawdown_pct=10.0), 1_000.0)
        rm.sync(1_200.0)                         # nuevo pico
        self.assertIsNone(rm.check_halt(1_150.0))
        halt = rm.check_halt(1_070.0)            # -10.8% desde 1200
        self.assertIsNotNone(halt)
        self.assertTrue(rm.state.halted)

    def test_el_halt_no_se_levanta_solo(self):
        rm = RiskManager(RiskConfig(max_total_drawdown_pct=10.0), 1_000.0)
        rm.check_halt(800.0)
        self.assertIsNotNone(rm.check_halt(1_500.0))   # sigue detenido

    def test_objetivo_total_de_ganancia(self):
        rm = RiskManager(RiskConfig(total_profit_target_pct=20.0), 1_000.0)
        self.assertIsNone(rm.check_halt(1_150.0))
        self.assertIn("objetivo total", rm.check_halt(1_210.0) or "")

    def test_detenido_rechaza_cualquier_entrada(self):
        rm = RiskManager(RiskConfig(max_total_drawdown_pct=10.0), 1_000.0)
        rm.check_halt(850.0)
        d = rm.evaluate_entry(mk_signal(), 850.0, [])
        self.assertFalse(d.allowed)
        self.assertIn("detenido", d.reason)


class TestSalidas(unittest.TestCase):
    def _pos(self, side=Side.LONG):
        return Position("TESTUSDT", side, 1.0, 100.0, 98.0, 104.0, now_ms())

    def test_stop_en_largo(self):
        rm = RiskManager(RiskConfig(trailing_stop_pct=0.0), 1_000.0)
        self.assertEqual(rm.check_exit(self._pos(), 97.5), ExitReason.STOP_LOSS)

    def test_take_profit_en_largo(self):
        rm = RiskManager(RiskConfig(trailing_stop_pct=0.0), 1_000.0)
        self.assertEqual(rm.check_exit(self._pos(), 104.5), ExitReason.TAKE_PROFIT)

    def test_si_la_vela_toca_ambos_gana_el_stop(self):
        rm = RiskManager(RiskConfig(trailing_stop_pct=0.0), 1_000.0)
        # Escenario pesimista deliberado: no sabemos el orden dentro de la vela.
        self.assertEqual(rm.check_exit(self._pos(), 101.0, high=105.0, low=97.0),
                         ExitReason.STOP_LOSS)

    def test_stop_en_corto_es_al_alza(self):
        rm = RiskManager(RiskConfig(trailing_stop_pct=0.0), 1_000.0)
        pos = Position("TESTUSDT", Side.SHORT, 1.0, 100.0, 102.0, 96.0, now_ms())
        self.assertEqual(rm.check_exit(pos, 102.5), ExitReason.STOP_LOSS)
        self.assertEqual(rm.check_exit(pos, 95.0), ExitReason.TAKE_PROFIT)

    def test_salida_por_tiempo(self):
        rm = RiskManager(RiskConfig(max_holding_bars=10, trailing_stop_pct=0.0), 1_000.0)
        pos = self._pos()
        pos.bars_held = 10
        self.assertEqual(rm.check_exit(pos, 100.0), ExitReason.TIME_STOP)

    def test_trailing_sube_pero_nunca_baja(self):
        rm = RiskManager(RiskConfig(trailing_stop_pct=2.0), 1_000.0)
        pos = self._pos()
        rm.update_position(pos, 110.0)
        subido = pos.stop_price
        self.assertAlmostEqual(subido, 107.8, places=4)
        rm.update_position(pos, 105.0)           # el precio retrocede
        self.assertAlmostEqual(pos.stop_price, subido, places=6)  # el stop no afloja

    def test_breakeven_protege_el_capital(self):
        rm = RiskManager(RiskConfig(breakeven_at_pct=1.0, trailing_stop_pct=0.0), 1_000.0)
        pos = self._pos()
        rm.update_position(pos, 101.5)
        self.assertTrue(pos.breakeven_armed)
        self.assertAlmostEqual(pos.stop_price, 100.0, places=6)


class TestExposicion(unittest.TestCase):
    def test_maximo_de_posiciones_simultaneas(self):
        rm = RiskManager(RiskConfig(max_open_positions=2), 1_000.0)
        abiertas = [Position(f"S{i}USDT", Side.LONG, 1.0, 10.0, 9.0, 12.0, now_ms()) for i in range(2)]
        d = rm.evaluate_entry(mk_signal(), 1_000.0, abiertas)
        self.assertFalse(d.allowed)
        self.assertIn("maximo de posiciones", d.reason)

    def test_no_duplica_posicion_en_el_mismo_simbolo(self):
        rm = RiskManager(RiskConfig(), 1_000.0)
        abierta = [Position("TESTUSDT", Side.LONG, 1.0, 100.0, 98.0, 104.0, now_ms())]
        d = rm.evaluate_entry(mk_signal(), 1_000.0, abierta)
        self.assertFalse(d.allowed)
        self.assertIn("ya hay posicion", d.reason)

    def test_exposicion_agregada_limita(self):
        cfg = RiskConfig(max_exposure_pct=30.0, max_open_positions=10, max_position_pct=100.0,
                         risk_per_trade_pct=5.0, stop_loss_pct=1.0, use_atr_stops=False)
        rm = RiskManager(cfg, 1_000.0)
        abiertas = [Position("AUSDT", Side.LONG, 2.0, 100.0, 99.0, 103.0, now_ms())]  # 200 expuestos
        d = rm.evaluate_entry(mk_signal(), 1_000.0, abiertas)
        self.assertTrue(d.allowed)
        self.assertLessEqual(d.notional, 100.0 + 1e-6)   # solo quedan 100 de margen


class TestConfig(unittest.TestCase):
    def test_take_profit_menor_que_stop_es_invalido(self):
        cfg = Config()
        cfg.risk.stop_loss_pct = 3.0
        cfg.risk.take_profit_pct = 1.0
        with self.assertRaises(ValueError):
            cfg.validate()

    def test_live_requiere_confirmacion_explicita(self):
        cfg = Config()
        cfg.mode = "live"
        with self.assertRaises(ValueError):
            cfg.validate()
        cfg.dry_run_confirm = True
        cfg.validate()

    def test_drawdown_global_debe_superar_al_diario(self):
        cfg = Config()
        cfg.risk.max_daily_loss_pct = 20.0
        cfg.risk.max_total_drawdown_pct = 10.0
        with self.assertRaises(ValueError):
            cfg.validate()


if __name__ == "__main__":
    unittest.main()
