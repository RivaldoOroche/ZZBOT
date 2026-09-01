import unittest

from zzbot import indicators as ind


class TestIndicators(unittest.TestCase):
    def test_sma_valores_conocidos(self):
        self.assertEqual(ind.sma([1, 2, 3, 4, 5], 3), [None, None, 2.0, 3.0, 4.0])

    def test_ema_arranca_con_la_media_simple(self):
        out = ind.ema([1, 2, 3, 4, 5, 6], 3)
        self.assertIsNone(out[1])
        self.assertAlmostEqual(out[2], 2.0)          # (1+2+3)/3
        self.assertAlmostEqual(out[3], 4 * 0.5 + 2 * 0.5)

    def test_rsi_en_subida_constante_es_100(self):
        self.assertAlmostEqual(ind.last_valid(ind.rsi(list(range(1, 40)), 14)), 100.0)

    def test_rsi_en_bajada_constante_es_0(self):
        self.assertAlmostEqual(ind.last_valid(ind.rsi(list(range(40, 1, -1)), 14)), 0.0)

    def test_rsi_esta_siempre_entre_0_y_100(self):
        import random
        rng = random.Random(3)
        closes = [100 + rng.gauss(0, 2) for _ in range(300)]
        for v in ind.rsi(closes, 14):
            if v is not None:
                self.assertGreaterEqual(v, 0.0)
                self.assertLessEqual(v, 100.0)

    def test_atr_es_positivo_y_alineado(self):
        highs = [10 + i * 0.1 for i in range(60)]
        lows = [9 + i * 0.1 for i in range(60)]
        closes = [9.5 + i * 0.1 for i in range(60)]
        out = ind.atr(highs, lows, closes, 14)
        self.assertEqual(len(out), 60)
        self.assertGreater(ind.last_valid(out), 0)

    def test_todas_las_series_conservan_la_longitud(self):
        closes = [100 + (i % 7) for i in range(200)]
        highs = [c + 1 for c in closes]
        lows = [c - 1 for c in closes]
        for series in (
            ind.sma(closes, 20), ind.ema(closes, 20), ind.rsi(closes, 14),
            ind.stdev(closes, 20), ind.zscore(closes, 20),
            ind.atr(highs, lows, closes, 14), ind.adx(highs, lows, closes, 14),
            ind.macd(closes)[0], ind.macd(closes)[1], ind.pct_change(closes, 10),
        ):
            self.assertEqual(len(series), 200)

    def test_bollinger_ordena_bandas(self):
        closes = [100 + (i % 11) for i in range(100)]
        mid, up, lo = ind.bollinger(closes, 20, 2.0)
        for i in range(len(closes)):
            if mid[i] is not None:
                self.assertLess(lo[i], mid[i])
                self.assertLess(mid[i], up[i])

    def test_adx_alto_en_tendencia_y_bajo_en_rango(self):
        n = 200
        tend = [100 + i for i in range(n)]
        adx_tend = ind.last_valid(ind.adx(tend, [c - 1 for c in tend], tend, 14))
        rango = [100 + (i % 4) for i in range(n)]
        adx_rango = ind.last_valid(ind.adx([c + 1 for c in rango], [c - 1 for c in rango], rango, 14))
        self.assertGreater(adx_tend, adx_rango)

    def test_periodo_invalido_falla(self):
        with self.assertRaises(ValueError):
            ind.sma([1, 2, 3], 0)

    def test_series_corta_devuelve_none(self):
        self.assertEqual(ind.ema([1, 2], 10), [None, None])
        self.assertIsNone(ind.last_valid(ind.rsi([1, 2, 3], 14)))


if __name__ == "__main__":
    unittest.main()
