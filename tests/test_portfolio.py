import unittest

from zzbot.config import ExecutionConfig
from zzbot.models import ExitReason, Side
from zzbot.portfolio import Portfolio


def mk(fees=0.0, slip=0.0):
    return Portfolio(cash=1000.0, execution=ExecutionConfig(taker_fee_pct=fees, slippage_pct=slip))


class TestContabilidad(unittest.TestCase):
    def test_el_efectivo_cuadra_en_un_ciclo_completo(self):
        p = mk()
        pos = p.open_position("XUSDT", Side.LONG, 2.0, 100.0, 98.0, 104.0)
        self.assertAlmostEqual(p.cash, 800.0)
        t = p.close_position(pos, 110.0, ExitReason.TAKE_PROFIT)
        self.assertAlmostEqual(p.cash, 1020.0)
        self.assertAlmostEqual(t.pnl, 20.0)

    def test_las_comisiones_se_descuentan_de_ambos_lados(self):
        p = mk(fees=0.1)
        pos = p.open_position("XUSDT", Side.LONG, 1.0, 100.0, 98.0, 104.0)
        t = p.close_position(pos, 100.0, ExitReason.MANUAL)
        # Entrar y salir al mismo precio deja una perdida igual a las dos comisiones.
        self.assertAlmostEqual(t.fees, 0.2, places=6)
        self.assertAlmostEqual(t.pnl, -0.2, places=6)
        self.assertAlmostEqual(p.cash, 999.8, places=6)

    def test_el_deslizamiento_siempre_va_en_contra(self):
        p = mk(slip=1.0)
        pos = p.open_position("XUSDT", Side.LONG, 1.0, 100.0, 98.0, 104.0)
        self.assertAlmostEqual(pos.entry_price, 101.0)     # compras mas caro
        t = p.close_position(pos, 100.0, ExitReason.MANUAL)
        self.assertAlmostEqual(t.exit_price, 99.0)         # vendes mas barato
        self.assertLess(t.pnl, 0)

    def test_rechaza_abrir_sin_efectivo(self):
        p = mk()
        self.assertIsNone(p.open_position("XUSDT", Side.LONG, 100.0, 100.0, 98.0, 104.0))
        self.assertAlmostEqual(p.cash, 1000.0)

    def test_equity_refleja_el_precio_de_mercado(self):
        p = mk()
        p.open_position("XUSDT", Side.LONG, 2.0, 100.0, 98.0, 104.0)
        self.assertAlmostEqual(p.mark_to_market({"XUSDT": 100.0}), 1000.0)
        self.assertAlmostEqual(p.mark_to_market({"XUSDT": 120.0}), 1040.0)
        self.assertAlmostEqual(p.mark_to_market({"XUSDT": 80.0}), 960.0)

    def test_corto_gana_cuando_el_precio_baja(self):
        p = mk()
        pos = p.open_position("XUSDT", Side.SHORT, 1.0, 100.0, 102.0, 96.0)
        self.assertAlmostEqual(p.mark_to_market({"XUSDT": 100.0}), 1000.0)
        self.assertAlmostEqual(p.mark_to_market({"XUSDT": 90.0}), 1010.0)
        t = p.close_position(pos, 90.0, ExitReason.TAKE_PROFIT)
        self.assertAlmostEqual(t.pnl, 10.0)
        self.assertAlmostEqual(p.cash, 1010.0)

    def test_estadisticas(self):
        p = mk()
        for exit_price in (110.0, 90.0, 105.0):
            pos = p.open_position("XUSDT", Side.LONG, 1.0, 100.0, 98.0, 104.0)
            p.close_position(pos, exit_price, ExitReason.MANUAL)
        s = p.stats(p.mark_to_market({}), 1000.0)
        self.assertEqual(s["operaciones"], 3)
        self.assertEqual(s["ganadoras"], 2)
        self.assertAlmostEqual(s["acierto_pct"], 66.67, places=1)
        self.assertAlmostEqual(s["profit_factor"], 1.5, places=3)


if __name__ == "__main__":
    unittest.main()
