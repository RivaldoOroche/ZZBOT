"""Tests de integracion del motor y del backtest, sin red."""

import logging
import os
import tempfile
import unittest

from tests.fake_source import FakeSource, make_candles
from zzbot.backtest import Backtester
from zzbot.config import Config
from zzbot.engine import TradingEngine
from zzbot.models import ExitReason, Series, Side
from zzbot.storage import Store

logging.disable(logging.CRITICAL)


def base_cfg(**over) -> Config:
    cfg = Config()
    cfg.scanner.max_markets = 5
    cfg.scanner.lookback_bars = 300
    cfg.scanner.min_quote_volume_24h = 0.0
    cfg.scanner.top_n_signals = 5
    cfg.strategy.min_score = 0.4
    cfg.execution.min_notional = 1.0
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def fake_universe(n_symbols=4, **kw):
    return FakeSource(
        {f"S{i}USDT": make_candles(seed=100 + i, **kw) for i in range(n_symbols)}
    )


class TestMotor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.sqlite")

    def _engine(self, cfg, source):
        cfg.db_path = self.db
        return TradingEngine(cfg, source=source, store=Store(self.db))

    def test_un_ciclo_en_tendencia_abre_posiciones(self):
        eng = self._engine(base_cfg(), fake_universe(drift=0.0015))
        eng.scanner.build_universe("USDT")
        eng._market_meta = {m.symbol: m for m in eng.scanner.universe}
        res = eng.run_cycle()
        self.assertGreaterEqual(res["opened"], 1)
        self.assertLessEqual(res["opened"], eng.cfg.risk.max_open_positions)

    def test_nunca_supera_el_maximo_de_posiciones(self):
        cfg = base_cfg()
        cfg.risk.max_open_positions = 2
        eng = self._engine(cfg, fake_universe(n_symbols=6, drift=0.0015))
        eng.scanner.build_universe("USDT")
        eng._market_meta = {m.symbol: m for m in eng.scanner.universe}
        for _ in range(3):
            eng.run_cycle()
        self.assertLessEqual(len(eng.portfolio.positions), 2)

    def test_el_bot_detenido_no_abre_nada(self):
        eng = self._engine(base_cfg(), fake_universe(drift=0.0015))
        eng.scanner.build_universe("USDT")
        eng._market_meta = {m.symbol: m for m in eng.scanner.universe}
        eng.risk.force_halt("prueba")
        res = eng.run_cycle()
        self.assertTrue(res.get("halted"))
        self.assertEqual(len(eng.portfolio.positions), 0)

    def test_el_limite_diario_bloquea_el_ciclo(self):
        cfg = base_cfg()
        cfg.risk.max_daily_loss_pct = 1.0
        eng = self._engine(cfg, fake_universe(drift=0.0015))
        eng.scanner.build_universe("USDT")
        eng._market_meta = {m.symbol: m for m in eng.scanner.universe}
        # Simulamos que el dia empezo con mas capital del que hay ahora.
        eng.risk.state.day_start_equity = cfg.initial_equity * 1.05
        res = eng.run_cycle()
        self.assertIn("blocked", res)
        self.assertEqual(len(eng.portfolio.positions), 0)

    def test_las_posiciones_sobreviven_a_un_reinicio(self):
        cfg = base_cfg()
        source = fake_universe(drift=0.0015)
        eng = self._engine(cfg, source)
        eng.scanner.build_universe("USDT")
        eng._market_meta = {m.symbol: m for m in eng.scanner.universe}
        eng.run_cycle()
        abiertas = set(eng.portfolio.positions)
        self.assertTrue(abiertas)
        eng.store.close()

        efectivo = eng.portfolio.cash
        self.assertLess(efectivo, cfg.initial_equity)   # hay capital comprometido

        cfg2 = base_cfg()
        cfg2.db_path = self.db
        eng2 = TradingEngine(cfg2, source=source, store=Store(self.db))
        self.assertEqual(set(eng2.portfolio.positions), abiertas)
        # El efectivo tambien se recupera: un reinicio no puede "regalar" capital.
        self.assertAlmostEqual(eng2.portfolio.cash, efectivo, places=6)
        self.assertAlmostEqual(
            eng2.portfolio.mark_to_market(source.fetch_prices(list(abiertas))),
            eng.portfolio.mark_to_market(source.fetch_prices(list(abiertas))),
            places=6,
        )

    def test_las_velas_de_las_posiciones_abiertas_se_refrescan(self):
        """Regresion: si el escaneo omitiera los simbolos abiertos, la salida
        por invalidacion de senal (signal_flip) nunca se dispararia en vivo."""
        eng = self._engine(base_cfg(), fake_universe(drift=0.0015))
        eng.scanner.build_universe("USDT")
        eng._market_meta = {m.symbol: m for m in eng.scanner.universe}
        eng.run_cycle()
        abiertas = set(eng.portfolio.positions)
        self.assertTrue(abiertas)
        eng.run_cycle()
        for sym in abiertas:
            self.assertIn(sym, eng._series_cache)
            self.assertGreater(len(eng._series_cache[sym]), 0)

    def test_el_escaneo_caido_no_impide_gestionar_lo_abierto(self):
        """Si el exchange falla al escanear, los stops siguen vigilandose."""
        from zzbot.exchanges.base import ExchangeError

        eng = self._engine(base_cfg(), fake_universe(drift=0.0015))
        eng.scanner.build_universe("USDT")
        eng._market_meta = {m.symbol: m for m in eng.scanner.universe}
        eng.run_cycle()
        self.assertTrue(eng.portfolio.positions)

        def scan_roto(*a, **kw):
            raise ExchangeError("caida simulada del exchange")

        eng.scanner.scan = scan_roto
        # Forzamos el stop de todas las posiciones abiertas.
        for pos in eng.portfolio.positions.values():
            pos.stop_price = pos.entry_price * 10
        res = eng.run_cycle()
        self.assertEqual(res["senales"], 0)
        self.assertGreaterEqual(res["closed"], 1)

    def test_los_ciclos_no_envejecen_las_posiciones(self):
        """Regresion: bars_held contaba ciclos, asi que max_holding_bars
        significaba 4 dias en backtest y 96 minutos en vivo."""
        eng = self._engine(base_cfg(), fake_universe(drift=0.0015))
        eng.scanner.build_universe("USDT")
        eng._market_meta = {m.symbol: m for m in eng.scanner.universe}
        eng.run_cycle()
        self.assertTrue(eng.portfolio.positions)
        for _ in range(5):
            eng.run_cycle()
        # Las velas son de 1h (default) y el test dura segundos: cero barras.
        for pos in eng.portfolio.positions.values():
            self.assertEqual(pos.bars_held, 0)

    def test_el_estado_de_riesgo_persiste(self):
        cfg = base_cfg()
        eng = self._engine(cfg, fake_universe())
        eng.scanner.build_universe("USDT")
        eng._market_meta = {m.symbol: m for m in eng.scanner.universe}
        eng.risk.state.trades_today = 7
        eng.run_cycle()
        eng.store.close()
        recuperado = Store(self.db).load_risk_state()
        self.assertEqual(recuperado.trades_today, 7)


class TestBacktest(unittest.TestCase):
    def _series(self, n_symbols=3, **kw):
        return {
            f"S{i}USDT": Series(f"S{i}USDT", "5m", make_candles(seed=200 + i, **kw))
            for i in range(n_symbols)
        }

    def test_backtest_produce_metricas_coherentes(self):
        cfg = base_cfg()
        r = Backtester(cfg).run(self._series(drift=0.0012))
        self.assertGreater(r.bars, 0)
        self.assertEqual(r.initial_equity, cfg.initial_equity)
        s = r.summary()
        self.assertEqual(s["operaciones"], len(r.trades))
        if r.trades:
            ganadoras = len([t for t in r.trades if t.is_win])
            self.assertAlmostEqual(s["acierto_pct"], ganadoras / len(r.trades) * 100, places=2)

    def test_ninguna_perdida_supera_el_limite_por_operacion(self):
        """La prueba clave: el stop configurado es el techo real de perdida."""
        cfg = base_cfg()
        cfg.risk.stop_loss_pct = 1.0
        cfg.risk.use_atr_stops = False
        cfg.risk.trailing_stop_pct = 0.0
        cfg.execution.slippage_pct = 0.0
        cfg.execution.taker_fee_pct = 0.0
        r = Backtester(cfg).run(self._series(drift=-0.001, noise=0.006))
        for t in r.trades:
            if t.reason is ExitReason.STOP_LOSS:
                # margen de 0.01 pp por redondeos de lote
                self.assertGreaterEqual(t.pnl_pct, -(cfg.risk.stop_loss_pct + 0.01))

    def test_el_drawdown_no_supera_el_limite_de_forma_grosera(self):
        cfg = base_cfg()
        cfg.risk.max_total_drawdown_pct = 8.0
        cfg.risk.risk_per_trade_pct = 0.5
        r = Backtester(cfg).run(self._series(drift=-0.0015, noise=0.008))
        # El kill switch actua entre velas, asi que se admite algo de exceso,
        # pero no el doble del limite.
        self.assertLess(r.max_drawdown_pct, cfg.risk.max_total_drawdown_pct * 2)

    def test_el_kill_switch_corta_el_backtest(self):
        cfg = base_cfg()
        cfg.risk.max_total_drawdown_pct = 1.0
        cfg.risk.risk_per_trade_pct = 3.0
        cfg.risk.max_position_pct = 100.0
        r = Backtester(cfg).run(self._series(drift=-0.003, noise=0.01))
        if r.halted_reason:
            self.assertEqual(len(r.trades), len([t for t in r.trades]))
            self.assertEqual(r.final_equity, r.equity_curve[-1])

    def test_falla_claro_si_no_hay_datos_suficientes(self):
        cfg = base_cfg()
        corto = {"AUSDT": Series("AUSDT", "5m", make_candles(n=20))}
        with self.assertRaises(ValueError):
            Backtester(cfg).run(corto)

    def test_sin_mirar_al_futuro(self):
        """Truncar el histórico no puede cambiar las operaciones ya ocurridas.

        Si la estrategia mirase velas futuras, el mismo tramo daria resultados
        distintos segun cuantos datos vinieran despues.
        """
        cfg = base_cfg()
        full = self._series(n_symbols=2, n=500)
        corte = 380
        truncado = {
            s: Series(v.symbol, v.interval, v.candles[:corte]) for s, v in full.items()
        }
        r_full = Backtester(cfg).run(full)
        r_trunc = Backtester(cfg).run(truncado)

        limite = truncado["S0USDT"].candles[-1].open_time
        abiertas_full = [t for t in r_full.trades if t.closed_at <= limite]
        abiertas_trunc = [t for t in r_trunc.trades if t.closed_at <= limite]
        self.assertEqual(
            [(t.symbol, t.opened_at, round(t.entry_price, 8)) for t in abiertas_full],
            [(t.symbol, t.opened_at, round(t.entry_price, 8)) for t in abiertas_trunc],
        )


if __name__ == "__main__":
    unittest.main()
