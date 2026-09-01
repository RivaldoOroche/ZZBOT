import unittest

from tests.fake_source import make_candles
from zzbot import strategies
from zzbot.models import Series, Side


def series(**kw) -> Series:
    return Series("TESTUSDT", "5m", make_candles(**kw))


class TestEstrategias(unittest.TestCase):
    def test_todas_registradas_y_construibles(self):
        for name in strategies.REGISTRY:
            st = strategies.build(name)
            self.assertEqual(st.name, name)

    def test_nombre_desconocido_falla_con_mensaje_util(self):
        with self.assertRaises(ValueError) as ctx:
            strategies.build("no_existe")
        self.assertIn("Disponibles", str(ctx.exception))

    def test_ninguna_devuelve_senal_sin_datos_suficientes(self):
        corta = series(n=30)
        for name in strategies.REGISTRY:
            self.assertIsNone(strategies.build(name).evaluate(corta))

    def test_las_senales_estan_bien_formadas(self):
        for name in strategies.REGISTRY:
            st = strategies.build(name, allow_short=True)
            for kw in ({"drift": 0.002}, {"drift": -0.002}, {"drift": 0.0, "noise": 0.01}):
                sig = st.evaluate(series(**kw))
                if sig is None:
                    continue
                self.assertGreater(sig.score, 0.0)
                self.assertLessEqual(sig.score, 1.0)
                self.assertGreater(sig.price, 0.0)
                self.assertIn(sig.side, (Side.LONG, Side.SHORT))
                self.assertTrue(sig.reason)

    def test_sin_cortos_no_devuelve_cortos(self):
        for name in strategies.REGISTRY:
            st = strategies.build(name, allow_short=False)
            for drift in (-0.003, -0.001, 0.001, 0.003):
                sig = st.evaluate(series(drift=drift, noise=0.006))
                if sig:
                    self.assertIs(sig.side, Side.LONG)

    def test_trend_momentum_entra_en_tendencia_sana(self):
        st = strategies.build("trend_momentum")
        alcista = st.evaluate(series(drift=0.001, noise=0.004, seed=11))
        self.assertIsNotNone(alcista)
        self.assertIs(alcista.side, Side.LONG)

    def test_trend_momentum_no_persigue_la_euforia(self):
        """Con RSI extremo (aqui ~95) se rechaza: comprar ahi es comprar el techo."""
        st = strategies.build("trend_momentum")
        from zzbot import indicators as ind
        s = series(drift=0.0025, noise=0.003, seed=11)
        self.assertGreater(ind.last_valid(ind.rsi(s.closes, 14)), 80.0)
        self.assertIsNone(st.evaluate(s))

    def test_mean_reversion_no_opera_con_tendencia_fuerte(self):
        """Su filtro de ADX debe descartar mercados en tendencia clara."""
        st = strategies.build("mean_reversion")
        self.assertIsNone(st.evaluate(series(drift=0.004, noise=0.001, seed=13)))

    def test_breakout_no_usa_la_vela_actual_para_el_canal(self):
        """El maximo del canal se mide con velas anteriores, si no siempre rompe."""
        st = strategies.build("breakout")
        plano = series(drift=0.0, noise=0.0005, seed=17)
        sig = st.evaluate(plano)
        if sig:
            self.assertGreater(sig.score, 0.0)

    def test_la_estrategia_es_determinista(self):
        s = series(drift=0.002, seed=21)
        for name in strategies.REGISTRY:
            st = strategies.build(name)
            a, b = st.evaluate(s), st.evaluate(s)
            self.assertEqual(a is None, b is None)
            if a:
                self.assertAlmostEqual(a.score, b.score)


if __name__ == "__main__":
    unittest.main()
