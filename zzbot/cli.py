"""Interfaz de linea de comandos de ZZBOT."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from typing import Dict, List, Optional

from . import __version__, strategies
from .backtest import Backtester
from .config import Config
from .engine import TradingEngine
from .exchanges.binance import BinancePublic
from .models import Series
from .scanner import Scanner
from .storage import Store

BANNER = r"""
 ZZBOT  bot de trading multimercado con limites de riesgo
 --------------------------------------------------------
 Por defecto opera en modo PAPER: precios reales, dinero simulado.
"""


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _fmt(value) -> str:
    if isinstance(value, float):
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    return str(value)


def _print_table(rows: List[Dict[str, object]], headers: List[str]) -> None:
    if not rows:
        print("  (sin datos)")
        return
    widths = {h: max(len(h), *(len(_fmt(r.get(h, ""))) for r in rows)) for h in headers}
    print("  " + "  ".join(h.ljust(widths[h]) for h in headers))
    print("  " + "  ".join("-" * widths[h] for h in headers))
    for r in rows:
        print("  " + "  ".join(_fmt(r.get(h, "")).ljust(widths[h]) for h in headers))


def _print_kv(title: str, data: Dict[str, object]) -> None:
    print(f"\n{title}")
    print("-" * len(title))
    width = max((len(k) for k in data), default=0)
    for k, v in data.items():
        print(f"  {k.ljust(width)} : {_fmt(v)}")


# ----------------------------------------------------------------------
# comandos
# ----------------------------------------------------------------------


def cmd_scan(args, cfg: Config) -> int:
    source = BinancePublic(workers=cfg.scanner.workers)
    scanner = Scanner(source, cfg.scanner)
    scanner.build_universe(cfg.execution.quote_asset)
    strategy = strategies.build(cfg.strategy.name, cfg.strategy.params, cfg.strategy.allow_short)

    print(f"\nEscaneando {len(scanner.universe)} mercados en {cfg.scanner.interval} "
          f"con la estrategia '{strategy.name}'...\n")
    result = scanner.scan(strategy)

    rows = [
        {
            "simbolo": s.symbol,
            "lado": s.side.value,
            "score": round(s.score, 3),
            "precio": s.price,
            "atr%": round(s.meta.get("atr_pct", 0.0), 3),
            "motivo": s.reason,
        }
        for s in result.signals
    ]
    _print_table(rows, ["simbolo", "lado", "score", "precio", "atr%", "motivo"])
    print(f"\n  {result.scanned} mercados analizados, {len(result.signals)} senales "
          f"(score minimo para operar: {cfg.strategy.min_score})")
    return 0


def cmd_run(args, cfg: Config) -> int:
    if cfg.mode == "live":
        print("\n*** MODO LIVE: se operara con DINERO REAL ***")
        print("La ejecucion real requiere un broker con claves de API configurado.")
        print("Esta version incluye el motor y los limites de riesgo, pero el broker")
        print("live no viene implementado a proposito: implementalo tu y pruebalo")
        print("primero en el testnet del exchange.\n")
        return 2

    engine = TradingEngine(cfg)
    try:
        engine.run(max_cycles=args.cycles)
    except KeyboardInterrupt:
        print("\ninterrumpido por el usuario")
    finally:
        equity = engine.portfolio.mark_to_market(engine.prices_for_open())
        _print_kv("ESTADO DE RIESGO", engine.risk.status(equity))
        _print_kv("RESULTADO", engine.portfolio.stats(equity, cfg.initial_equity))
    return 0


def cmd_backtest(args, cfg: Config) -> int:
    source = BinancePublic(workers=cfg.scanner.workers)
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        scanner = Scanner(source, cfg.scanner)
        symbols = [m.symbol for m in scanner.build_universe(cfg.execution.quote_asset)]
    symbols = symbols[: args.max_symbols] if args.max_symbols else symbols

    start_ms = int((time.time() - args.days * 86400) * 1000)
    print(f"\nDescargando {args.days} dias de {cfg.scanner.interval} para {len(symbols)} mercados...")
    series_map: Dict[str, Series] = {}
    for i, sym in enumerate(symbols, 1):
        try:
            s = source.fetch_historical(sym, cfg.scanner.interval, start_ms)
            if len(s) > 0:
                series_map[sym] = s
            print(f"  [{i}/{len(symbols)}] {sym}: {len(s)} velas")
        except Exception as exc:  # noqa: BLE001
            print(f"  [{i}/{len(symbols)}] {sym}: error ({exc})")

    if not series_map:
        print("no se pudo descargar ningun historico")
        return 1

    logging.getLogger("zzbot.portfolio").setLevel(logging.WARNING)
    result = Backtester(cfg).run(series_map)

    _print_kv("BACKTEST", result.summary())
    _print_kv(
        "LIMITES APLICADOS",
        {
            "stop por operacion %": cfg.risk.stop_loss_pct,
            "take profit por operacion %": cfg.risk.take_profit_pct,
            "riesgo por operacion %": cfg.risk.risk_per_trade_pct,
            "perdida maxima diaria %": cfg.risk.max_daily_loss_pct,
            "ganancia objetivo diaria %": cfg.risk.daily_profit_target_pct,
            "drawdown maximo %": cfg.risk.max_total_drawdown_pct,
            "posiciones simultaneas": cfg.risk.max_open_positions,
        },
    )

    if result.trades:
        print("\nULTIMAS OPERACIONES")
        print("-" * 19)
        rows = [
            {
                "simbolo": t.symbol,
                "lado": t.side.value,
                "entrada": round(t.entry_price, 8),
                "salida": round(t.exit_price, 8),
                "pnl": round(t.pnl, 4),
                "pnl%": round(t.pnl_pct, 2),
                "motivo": t.reason.value,
            }
            for t in result.trades[-15:]
        ]
        _print_table(rows, ["simbolo", "lado", "entrada", "salida", "pnl", "pnl%", "motivo"])

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "resumen": result.summary(),
                    "config": cfg.to_dict(),
                    "curva_equity": result.equity_curve,
                    "timestamps": result.timestamps,
                },
                fh,
                indent=2,
                default=str,
            )
        print(f"\nresultados guardados en {args.json}")

    print("\nRecuerda: un backtest no predice el futuro. Mide si la configuracion")
    print("de riesgo se comporta como esperas, no cuanto vas a ganar.")
    return 0


def cmd_status(args, cfg: Config) -> int:
    store = Store(cfg.db_path)
    trades = store.recent_trades(args.limit)
    equity_rows = store.equity_history(1)

    equity = equity_rows[0]["equity"] if equity_rows else cfg.initial_equity
    _print_kv(
        "ESTADO",
        {
            "modo": cfg.mode,
            "estrategia": cfg.strategy.name,
            "equity": round(equity, 2),
            "equity inicial": cfg.initial_equity,
            "resultado %": round((equity - cfg.initial_equity) / cfg.initial_equity * 100, 3),
            "operaciones registradas": store.trade_count(),
            "posiciones abiertas": len(store.load_positions()),
        },
    )
    state = store.load_risk_state()
    if state:
        _print_kv("RIESGO", state.to_dict())

    print("\nULTIMAS OPERACIONES")
    print("-" * 19)
    _print_table(
        [
            {
                "simbolo": r["symbol"],
                "lado": r["side"],
                "pnl": round(r["pnl"], 4),
                "pnl%": round(r["pnl_pct"], 2),
                "motivo": r["reason"],
            }
            for r in trades
        ],
        ["simbolo", "lado", "pnl", "pnl%", "motivo"],
    )
    store.close()
    return 0


def cmd_limits(args, cfg: Config) -> int:
    """Muestra los limites activos y simula su efecto sobre el capital."""
    eq = cfg.initial_equity
    r = cfg.risk
    _print_kv(
        "LIMITES DE RIESGO ACTIVOS",
        {
            "equity inicial": eq,
            "riesgo por operacion": f"{r.risk_per_trade_pct}%  ->  {eq * r.risk_per_trade_pct / 100:.2f}",
            "stop loss por operacion": f"{r.stop_loss_pct}%",
            "take profit por operacion": f"{r.take_profit_pct}%",
            "ratio beneficio/riesgo": round(r.take_profit_pct / r.stop_loss_pct, 2),
            "perdida maxima diaria": f"{r.max_daily_loss_pct}%  ->  -{eq * r.max_daily_loss_pct / 100:.2f}",
            "ganancia objetivo diaria": f"{r.daily_profit_target_pct}%  ->  +{eq * r.daily_profit_target_pct / 100:.2f}",
            "drawdown maximo (apaga el bot)": f"{r.max_total_drawdown_pct}%  ->  {eq * (1 - r.max_total_drawdown_pct / 100):.2f}",
            "objetivo total (apaga el bot)": (
                f"{r.total_profit_target_pct}%  ->  {eq * (1 + r.total_profit_target_pct / 100):.2f}"
                if r.total_profit_target_pct > 0 else "desactivado"
            ),
            "posiciones simultaneas": r.max_open_positions,
            "exposicion maxima": f"{r.max_exposure_pct}%  ->  {eq * r.max_exposure_pct / 100:.2f}",
            "operaciones maximas por dia": r.max_daily_trades,
            "racha de perdidas que pausa": r.max_consecutive_losses,
        },
    )
    losses_to_daily_stop = r.max_daily_loss_pct / r.risk_per_trade_pct
    print(f"\n  Con estos numeros hacen falta ~{losses_to_daily_stop:.0f} operaciones perdedoras")
    print(f"  seguidas para alcanzar el limite diario, y la racha de "
          f"{r.max_consecutive_losses} perdidas pausa antes.")
    win_needed = 100 / (1 + r.take_profit_pct / r.stop_loss_pct)
    print(f"  Con un ratio {r.take_profit_pct / r.stop_loss_pct:.2f}:1 necesitas acertar "
          f"mas del {win_needed:.1f}% de las veces solo para empatar (sin contar comisiones).")
    return 0


def cmd_init(args, cfg: Config) -> int:
    from .config import Config as C

    path = args.output
    data = C().to_dict()
    with open(path, "w", encoding="utf-8") as fh:
        if path.endswith((".yaml", ".yml")):
            import yaml  # type: ignore

            yaml.safe_dump(data, fh, sort_keys=False, allow_unicode=True)
        else:
            json.dump(data, fh, indent=2)
    print(f"config de ejemplo escrita en {path}")
    return 0


# ----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="zzbot",
        description="Bot de trading multimercado con limites de perdida y ganancia configurables.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=BANNER,
    )
    p.add_argument("-c", "--config", help="ruta a config .yaml o .json")
    p.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING, ERROR")
    p.add_argument("--version", action="version", version=f"zzbot {__version__}")

    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("scan", help="escanear mercados y mostrar senales, sin operar")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("run", help="ejecutar el bot (paper por defecto)")
    s.add_argument("--cycles", type=int, default=None, help="numero de ciclos y salir")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("backtest", help="probar la configuracion contra el historico")
    s.add_argument("--days", type=int, default=30, help="dias de historico (por defecto 30)")
    s.add_argument("--symbols", help="lista separada por comas, ej: BTCUSDT,ETHUSDT")
    s.add_argument("--max-symbols", type=int, default=10, help="tope de mercados a descargar")
    s.add_argument("--json", help="guardar resultados en este archivo")
    s.set_defaults(func=cmd_backtest)

    s = sub.add_parser("status", help="ver estado guardado y ultimas operaciones")
    s.add_argument("--limit", type=int, default=15)
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("limits", help="ver los limites activos traducidos a dinero")
    s.set_defaults(func=cmd_limits)

    s = sub.add_parser("init", help="generar un archivo de configuracion de ejemplo")
    s.add_argument("-o", "--output", default="config.yaml")
    s.set_defaults(func=cmd_init)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        cfg = Config.load(args.config)
    except (ValueError, OSError) as exc:
        print(f"error de configuracion: {exc}", file=sys.stderr)
        return 1
    setup_logging(args.log_level or cfg.log_level)
    return args.func(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
