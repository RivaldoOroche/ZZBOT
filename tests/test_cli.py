"""Tests del CLI que no tocan la red."""

import contextlib
import io
import json
import os
import tempfile
import unittest

from zzbot import cli, strategies
from zzbot.config import Config


def run(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.main(argv)
    return code, out.getvalue()


class TestCLI(unittest.TestCase):
    def test_todos_los_subcomandos_estan_registrados(self):
        parser = cli.build_parser()
        acciones = [a for a in parser._actions if a.dest == "command"]
        self.assertEqual(
            set(acciones[0].choices),
            {"scan", "run", "backtest", "compare", "status", "limits", "init"},
        )

    def test_limits_traduce_los_porcentajes_a_dinero(self):
        code, out = run(["limits"])
        self.assertEqual(code, 0)
        self.assertIn("perdida maxima diaria", out)
        self.assertIn("drawdown maximo", out)
        self.assertIn("-30.00", out)      # 3% de 1000
        self.assertIn("+50.00", out)      # 5% de 1000

    def test_init_genera_una_config_recargable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "c.json")
            code, _ = run(["init", "-o", path])
            self.assertEqual(code, 0)
            with open(path) as fh:
                self.assertIn("risk", json.load(fh))
            Config.load(path).validate()      # debe recargar sin errores

    def test_una_config_invalida_falla_con_codigo_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "malo.json")
            with open(path, "w") as fh:
                json.dump({"risk": {"stop_loss_pct": 5.0, "take_profit_pct": 1.0}}, fh)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code, _ = run(["-c", path, "limits"])
            self.assertEqual(code, 1)
            self.assertIn("error de configuracion", err.getvalue())

    def test_live_sin_broker_no_arranca(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "live.json")
            with open(path, "w") as fh:
                json.dump({"mode": "live", "dry_run_confirm": True}, fh)
            code, out = run(["-c", path, "run"])
            self.assertEqual(code, 2)
            self.assertIn("DINERO REAL", out)

    def test_compare_rechaza_una_estrategia_desconocida(self):
        code, out = run(["compare", "--strategies", "no_existe"])
        self.assertEqual(code, 1)
        self.assertIn("desconocida", out)

    def test_la_tabla_soporta_filas_vacias(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli._print_table([], ["a", "b"])
        self.assertIn("sin datos", out.getvalue())


if __name__ == "__main__":
    unittest.main()
