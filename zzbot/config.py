"""Configuracion del bot. Todo lo ajustable vive aqui.

Se carga desde YAML o JSON. Cualquier campo puede sobreescribirse con variables
de entorno con prefijo ZZBOT_ y rutas separadas por doble guion bajo, por ejemplo:

    ZZBOT_RISK__MAX_DAILY_LOSS_PCT=1.5
    ZZBOT_MODE=paper
"""

from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Dict, List, Optional, get_type_hints

ENV_PREFIX = "ZZBOT_"


@dataclass
class RiskConfig:
    """Limites de riesgo. Estos son los frenos del bot.

    Los porcentajes se expresan en tanto por ciento (1.5 == 1.5%).
    """

    # --- Limites por operacion ---
    stop_loss_pct: float = 1.2
    """Perdida maxima aceptada en una sola operacion, en % del precio de entrada."""

    take_profit_pct: float = 2.4
    """Ganancia objetivo en una sola operacion, en % del precio de entrada."""

    use_atr_stops: bool = True
    """Si es True, el stop y el take profit se calculan con ATR (volatilidad real)
    y los porcentajes de arriba actuan solo como topes de seguridad."""

    atr_stop_mult: float = 1.8
    atr_take_profit_mult: float = 3.2

    trailing_stop_pct: float = 0.0
    """0 desactiva el trailing. Si es > 0, el stop sigue al precio a esa distancia
    una vez la operacion esta en ganancia."""

    breakeven_at_pct: float = 0.0
    """Mueve el stop a punto de entrada cuando la ganancia flotante alcanza este %.
    0 lo desactiva."""

    max_holding_bars: int = 96
    """Cierre forzado por tiempo. Evita quedarse atrapado en una posicion muerta."""

    # --- Dimensionamiento de posicion ---
    risk_per_trade_pct: float = 0.5
    """Porcentaje del equity que se arriesga por operacion. El tamano se deduce
    de esto y de la distancia al stop, no al reves."""

    max_position_pct: float = 20.0
    """Tope duro al valor de una posicion como % del equity."""

    max_open_positions: int = 5
    max_exposure_pct: float = 60.0
    """Suma maxima del valor de todas las posiciones abiertas, como % del equity."""

    max_positions_per_symbol: int = 1

    # --- Limites de sesion / diarios ---
    max_daily_loss_pct: float = 3.0
    """RANGO MAXIMO DE PERDIDA DIARIO. Al tocarlo el bot deja de abrir posiciones
    durante el resto del dia UTC."""

    daily_profit_target_pct: float = 5.0
    """GANANCIA MAXIMA DIARIA. Al alcanzarla el bot deja de operar ese dia:
    proteger lo ganado vale mas que forzar una operacion extra."""

    max_daily_trades: int = 40
    max_consecutive_losses: int = 5
    """Racha de perdidas que activa una pausa. Suele indicar que el regimen de
    mercado cambio y la estrategia ya no aplica."""

    cooldown_minutes_after_stop: int = 60
    """Pausa tras alcanzar un limite diario o una racha de perdidas."""

    # --- Limites globales (kill switch) ---
    max_total_drawdown_pct: float = 15.0
    """Caida maxima desde el pico de equity. Al tocarla el bot se apaga."""

    total_profit_target_pct: float = 0.0
    """Objetivo total de ganancia sobre el capital inicial. 0 lo desactiva."""

    close_positions_on_halt: bool = True
    """Si un limite duro se activa, cerrar tambien lo que este abierto."""


@dataclass
class ExecutionConfig:
    order_type: str = "market"          # market | limit
    limit_offset_pct: float = 0.05      # cuanto mejorar el precio en ordenes limit
    taker_fee_pct: float = 0.10         # comision estimada por lado
    maker_fee_pct: float = 0.02
    slippage_pct: float = 0.03          # deslizamiento asumido en simulacion
    min_notional: float = 10.0          # tamano minimo de orden en moneda quote
    quote_asset: str = "USDT"


@dataclass
class ScannerConfig:
    max_markets: int = 50
    """Cuantos mercados vigilar simultaneamente."""

    interval: str = "1h"
    """Temporalidad de las velas. En 5m las comisiones se comen la ventaja:
    con 0.1% por lado, cada operacion arranca ~0.2% en contra sobre un stop
    del 1.2%. Marcos mas largos operan menos y pagan menos peaje."""

    lookback_bars: int = 300
    min_quote_volume_24h: float = 20_000_000.0
    """Filtro de liquidez. Operar mercados ilíquidos es como regalar dinero al spread."""

    max_spread_pct: float = 0.15
    include: List[str] = field(default_factory=list)
    """Si tiene simbolos, solo se operan esos."""

    exclude: List[str] = field(default_factory=lambda: ["USDCUSDT", "FDUSDUSDT", "TUSDUSDT"])
    exclude_leveraged: bool = True      # descarta tokens UP/DOWN/BULL/BEAR
    workers: int = 8
    top_n_signals: int = 5


@dataclass
class StrategyConfig:
    name: str = "trend_momentum"
    """trend_momentum | mean_reversion | breakout"""

    allow_short: bool = False
    min_score: float = 0.55
    """Confianza minima de la senal para operarla (0 a 1)."""

    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class NotifyConfig:
    enabled: bool = False
    webhook_url: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    notify_on: List[str] = field(default_factory=lambda: ["open", "close", "halt"])


@dataclass
class Config:
    mode: str = "paper"
    """paper = simulado con precios reales (por defecto). live = dinero real."""

    exchange: str = "binance"
    initial_equity: float = 1000.0
    poll_seconds: int = 30
    dry_run_confirm: bool = False
    """Debe ponerse en True a mano para permitir mode=live. Es un seguro."""

    db_path: str = "zzbot_state.sqlite"
    log_level: str = "INFO"

    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    scanner: ScannerConfig = field(default_factory=ScannerConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)

    # --- carga ---

    @classmethod
    def load(cls, path: Optional[str] = None) -> "Config":
        raw: Dict[str, Any] = {}
        if path:
            raw = _read_file(path)
        cfg = _from_dict(cls, raw)
        _apply_env(cfg)
        cfg.validate()
        return cfg

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    def validate(self) -> None:
        errors: List[str] = []
        r = self.risk
        if self.mode not in ("paper", "live", "backtest"):
            errors.append(f"mode invalido: {self.mode}")
        if self.mode == "live" and not self.dry_run_confirm:
            errors.append(
                "mode=live requiere dry_run_confirm=true en la config. "
                "Es intencional: obliga a un acto deliberado antes de arriesgar dinero real."
            )
        if self.initial_equity <= 0:
            errors.append("initial_equity debe ser > 0")
        if r.stop_loss_pct <= 0:
            errors.append("risk.stop_loss_pct debe ser > 0")
        if r.take_profit_pct <= 0:
            errors.append("risk.take_profit_pct debe ser > 0")
        if r.take_profit_pct <= r.stop_loss_pct:
            errors.append(
                "risk.take_profit_pct debe superar a stop_loss_pct: con ratio <= 1 "
                "necesitas mas del 50% de aciertos solo para empatar, y las comisiones te comen."
            )
        if not 0 < r.risk_per_trade_pct <= 5:
            errors.append("risk.risk_per_trade_pct debe estar entre 0 y 5")
        if r.max_daily_loss_pct <= 0:
            errors.append("risk.max_daily_loss_pct debe ser > 0")
        if r.max_total_drawdown_pct <= r.max_daily_loss_pct:
            errors.append(
                "risk.max_total_drawdown_pct debe ser mayor que max_daily_loss_pct, "
                "si no el kill switch global salta el mismo dia que el limite diario."
            )
        if r.max_open_positions < 1:
            errors.append("risk.max_open_positions debe ser >= 1")
        if r.max_position_pct * r.max_open_positions < r.max_position_pct:
            errors.append("configuracion de exposicion incoherente")
        if self.scanner.max_markets < 1:
            errors.append("scanner.max_markets debe ser >= 1")
        if self.strategy.name not in ("trend_momentum", "mean_reversion", "breakout"):
            errors.append(f"strategy.name desconocida: {self.strategy.name}")
        if errors:
            raise ValueError("Config invalida:\n  - " + "\n  - ".join(errors))


def _read_file(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    if path.endswith((".yaml", ".yml")):
        try:
            import yaml  # type: ignore
        except ImportError as exc:  # pragma: no cover - depende del entorno
            raise RuntimeError(
                "Config YAML requiere pyyaml (pip install pyyaml) o usa un archivo .json"
            ) from exc
        return yaml.safe_load(text) or {}
    return json.loads(text)


def _from_dict(cls, data: Dict[str, Any]):
    """Construye el dataclass recursivamente y rechaza claves desconocidas.

    Un typo silencioso en la config de riesgo es exactamente el tipo de fallo que
    cuesta dinero, asi que preferimos fallar al arrancar.
    """
    if not isinstance(data, dict):
        raise ValueError(f"se esperaba un objeto para {cls.__name__}")
    # get_type_hints resuelve las anotaciones, que con `from __future__ import
    # annotations` llegan como cadenas.
    hints = get_type_hints(cls)
    known = {f.name for f in fields(cls)}
    kwargs: Dict[str, Any] = {}
    for key, value in data.items():
        if key not in known:
            raise ValueError(f"clave desconocida en config ({cls.__name__}): {key}")
        hint = hints.get(key)
        if isinstance(hint, type) and is_dataclass(hint):
            kwargs[key] = _from_dict(hint, value)
        else:
            kwargs[key] = value
    return cls(**kwargs)


def _coerce(current: Any, raw: str) -> Any:
    if isinstance(current, bool):
        return raw.strip().lower() in ("1", "true", "yes", "y", "on")
    if isinstance(current, int):
        return int(float(raw))
    if isinstance(current, float):
        return float(raw)
    if isinstance(current, list):
        return [p.strip() for p in raw.split(",") if p.strip()]
    return raw


def _apply_env(cfg: Config) -> None:
    for env_key, raw in os.environ.items():
        if not env_key.startswith(ENV_PREFIX):
            continue
        path = env_key[len(ENV_PREFIX) :].lower().split("__")
        target: Any = cfg
        for part in path[:-1]:
            if not hasattr(target, part):
                target = None
                break
            target = getattr(target, part)
        leaf = path[-1]
        if target is None or not hasattr(target, leaf):
            continue
        setattr(target, leaf, _coerce(getattr(target, leaf), raw))
