# ZZBOT

Bot de trading multimercado para cripto: escanea decenas de mercados en paralelo,
genera señales con estrategias configurables, y **ejecuta bajo límites de pérdida
y ganancia que tú defines**.

Escrito en Python puro, sin dependencias obligatorias (solo la librería estándar).

---

## Antes de nada: qué es real y qué no

Este proyecto nació de un post viral que decía que un bot convirtió **$68 en $750.000**.
Eso no es real. Vale la pena decirlo claro antes de que arriesgues dinero:

| La afirmación | La realidad |
|---|---|
| "$68 → $750.000" | Un retorno del 1.100.000%. Ningún fondo de la historia se acerca. Si una estrategia diera eso, no se publicaría en redes. |
| "Detecta errores de precio antes que los humanos" | El arbitraje real lo hacen firmas con servidores dentro del datacenter del exchange. Compiten en microsegundos. Un bot en Python sobre internet doméstica llega tarde, siempre. |
| "Arbitraje sin riesgo" | El arbitraje cripto accesible tiene márgenes por debajo de las comisiones. Cuando el margen es visible, ya se cerró. |
| "Funciona 24/7 sin intervención" | Esto sí es real, y es exactamente por lo que **los límites de riesgo importan más que la estrategia**. Un bot desatendido con un fallo puede vaciar una cuenta mientras duermes. |

**Lo que este bot sí hace de verdad:** escanear muchos mercados a la vez, aplicar
una estrategia con criterio, dimensionar posiciones por riesgo, y respetar límites
duros de pérdida y ganancia. Eso es lo que separa un bot utilizable de un generador
de pérdidas automatizado.

Arranca en modo **paper** (precios reales, dinero simulado). Es donde debe quedarse
hasta que tengas semanas de datos propios.

---

## Instalación

```bash
git clone <este-repo> && cd ZZBOT
python3 --version          # requiere 3.9 o superior
```

No hace falta `pip install` nada. `pyyaml` es opcional (solo si quieres config en
YAML en lugar de JSON).

---

## Uso rápido

```bash
# 1. Ver los límites activos traducidos a dinero real
python3 -m zzbot limits

# 2. Escanear el mercado ahora mismo, sin operar nada
python3 -m zzbot scan

# 3. Probar la configuración contra el histórico
python3 -m zzbot backtest --days 60 --symbols BTCUSDT,ETHUSDT,SOLUSDT

# 3b. Comparar estrategias y temporalidades para saber cuál aguanta
python3 -m zzbot compare --days 90

# 4. Ejecutar en modo paper (dinero simulado, precios reales)
python3 -m zzbot run

# 5. Ver el estado y las últimas operaciones
python3 -m zzbot status
```

Con configuración propia:

```bash
cp config.example.yaml config.yaml
# edita config.yaml
python3 -m zzbot -c config.yaml run
```

---

## Los límites (lo que pediste)

Todo se ajusta en la sección `risk:` de la config. Hay tres capas de frenos,
y ninguna estrategia puede saltárselas: el motor pregunta al gestor de riesgo
antes de abrir cualquier posición.

### Capa 1 — por operación

```yaml
risk:
  stop_loss_pct: 1.2        # pérdida máxima en UNA operación
  take_profit_pct: 2.4      # ganancia objetivo en UNA operación
  risk_per_trade_pct: 0.5   # % del capital arriesgado por operación
  trailing_stop_pct: 0.8    # el stop sigue al precio cuando vas ganando
  breakeven_at_pct: 1.0     # mueve el stop a la entrada con +1%
  max_holding_bars: 96      # cierre por tiempo
```

El tamaño de la posición **se deduce del riesgo, no al revés**. Si arriesgas 0,5%
de 1.000 (= 5) y el stop está a 1,2% de distancia, el bot compra 416 de nocional.
Cambias el stop, cambia el tamaño solo; lo que pierdes si salta el stop sigue siendo 5.

Con `use_atr_stops: true` los niveles se adaptan a la volatilidad real de cada
activo, pero `stop_loss_pct` **siempre actúa como techo**: por volátil que esté
un mercado, nunca arriesgas más de lo que autorizaste.

### Capa 2 — por día

```yaml
risk:
  max_daily_loss_pct: 3.0        # RANGO MÁXIMO DE PÉRDIDA DIARIO
  daily_profit_target_pct: 5.0   # GANANCIA MÁXIMA DIARIA
  max_consecutive_losses: 5      # racha que activa una pausa
  max_daily_trades: 40
  cooldown_minutes_after_stop: 60
```

Al tocar cualquiera de estos, el bot **deja de abrir posiciones** el resto del día
UTC. Sigue gestionando lo que ya está abierto (stops y take profit siguen vivos),
pero no busca nuevas entradas.

El objetivo de ganancia diaria parece raro al principio —¿por qué parar de ganar?—
pero es de los frenos más útiles: la mayoría de las cuentas no mueren por un mal
día, mueren por devolver un buen día.

### Capa 3 — global (kill switch)

```yaml
risk:
  max_total_drawdown_pct: 15.0   # caída máxima desde el pico -> APAGA el bot
  total_profit_target_pct: 0.0   # objetivo total -> APAGA el bot (0 = sin límite)
  close_positions_on_halt: true
```

Estos apagan el bot por completo. **Un halt no se levanta solo**: exige que mires
qué pasó y lo reinicies a mano. Es intencional.

Para ver qué significan tus números en dinero:

```bash
$ python3 -m zzbot limits

  riesgo por operacion           : 0.5%  ->  5.00
  perdida maxima diaria          : 3.0%  ->  -30.00
  ganancia objetivo diaria       : 5.0%  ->  +50.00
  drawdown maximo (apaga el bot) : 15.0%  ->  850.00

  Con estos numeros hacen falta ~6 operaciones perdedoras seguidas para
  alcanzar el limite diario, y la racha de 5 perdidas pausa antes.
  Con un ratio 2.00:1 necesitas acertar mas del 33.3% de las veces solo
  para empatar (sin contar comisiones).
```

---

## Estrategias

| Estrategia | Cuándo entra | Cuándo NO sirve |
|---|---|---|
| `trend_momentum` | EMA rápida sobre lenta, RSI en zona sana, ADX con tendencia, volumen confirmando | Mercados laterales: te sacan por stop una y otra vez |
| `mean_reversion` | Precio a 2+ desviaciones bajo su media, RSI sobrevendido, **ADX bajo** | Tendencias fuertes: "comprar barato" en una caída es cómo se pierde dinero rápido |
| `breakout` | Ruptura de máximos de N velas tras compresión de volatilidad, con volumen | Mercados sin compresión previa: la mayoría de rupturas son ruido |

Cada una devuelve un **score de 0 a 1** que suma las condiciones que se cumplen.
`strategy.min_score` decide cuánta confianza exiges antes de operar. Subirlo
significa menos operaciones y más selectivas.

Cambiar de estrategia no toca el gestor de riesgo. Esa separación es deliberada:
puedes experimentar con señales sin tocar los frenos.

### Añadir la tuya

```python
# zzbot/strategies/mi_estrategia.py
from .base import Strategy
from ..models import Side, Signal

class MiEstrategia(Strategy):
    name = "mi_estrategia"
    min_bars = 100

    def evaluate(self, series):
        if <tu condición>:
            return Signal(symbol=series.symbol, side=Side.LONG,
                          score=0.7, price=series.last_price,
                          atr=<atr>, reason="por qué entras")
        return None
```

Regístrala en `zzbot/strategies/__init__.py` y úsala con `strategy.name: mi_estrategia`.

---

## El escáner

```yaml
scanner:
  max_markets: 50               # cuántos mercados vigilar a la vez
  interval: 1h                  # ver "Qué dicen los datos": 5m pierde por comisiones
  min_quote_volume_24h: 20000000   # filtro de liquidez
  max_spread_pct: 0.15
```

Escanear 50 mercados no sirve de nada si 40 son ilíquidos: el spread se come el
margen antes de empezar. El bot filtra por volumen de 24h, spread y descarta
tokens apalancados (UP/DOWN/BULL/BEAR), y luego ordena las señales por score.

```bash
$ python3 -m zzbot scan

  simbolo   lado  score  precio     atr%   motivo
  UNIUSDT   long  0.91   5.383      0.612  tendencia alcista ema20>50, rsi=61, adx=35
  NEARUSDT  long  0.88   1.967      0.702  tendencia alcista ema20>50, rsi=61, adx=37
  LINKUSDT  long  0.74   11.469     0.531  tendencia alcista ema20>50, rsi=61, adx=24
```

---

## Backtest

```bash
python3 -m zzbot backtest --days 60 --symbols BTCUSDT,ETHUSDT,SOLUSDT --json out.json
```

Reproduce el histórico vela a vela **con la misma lógica que en vivo**. Las reglas
que hacen que el resultado no sea mentira:

- Las decisiones se toman solo con datos cerrados hasta la vela actual. Nunca mira al futuro.
- La estrategia ve la misma ventana de velas que vería en vivo (`lookback_bars`).
- Stops y take profit se evalúan contra el máximo y mínimo de cada vela.
- **Si una vela toca el stop y el take profit, se asume el stop.** No sabemos el
  orden dentro de la vela, así que asumimos lo peor. Un backtest que asume lo
  contrario infla los resultados de forma sistemática.
- Comisiones y deslizamiento se aplican siempre, en contra.

Aun así, un backtest **no predice el futuro**. No modela profundidad de libro,
huecos de precio ni caídas del exchange. Sirve para descartar configuraciones malas
—y para verificar que tus límites de riesgo se comportan como esperas—, no para
prometer ganancias.

---

## Qué dicen los datos (no las suposiciones)

Comparativa real sobre **90 días** (junio–septiembre 2026), 8 mercados
(BTC, ETH, SOL, BNB, XRP, DOGE, LINK, AVAX), con la configuración por defecto
de riesgo. Reproducible con:

```bash
python3 -m zzbot compare --days 90
```

| Temporalidad | Estrategia | Retorno | Drawdown máx | Ops | Acierto | Profit factor | Kill switch |
|---|---|---:|---:|---:|---:|---:|:--:|
| 1h | `mean_reversion` | **+2,63%** | 1,09% | 44 | 61,4% | 1,63 | |
| 4h | `breakout` | **+1,37%** | 3,92% | 69 | 43,5% | 1,12 | |
| 4h | `mean_reversion` | −1,19% | 1,63% | 9 | 22,2% | 0,42 | |
| 15m | `mean_reversion` | −3,98% | 6,35% | 97 | 39,2% | 0,63 | |
| 1h | `breakout` | −6,27% | 9,51% | 182 | 36,8% | 0,78 | |
| 4h | `trend_momentum` | −8,04% | 12,15% | 150 | 33,3% | 0,71 | |
| 15m | `breakout` | −13,78% | 15,16% | 259 | 34,4% | 0,57 | **SÍ** |
| 15m | `trend_momentum` | −13,81% | 14,90% | 139 | 23,0% | 0,35 | **SÍ** |
| 1h | `trend_momentum` | −13,99% | 14,98% | 191 | 29,3% | 0,58 | **SÍ** |

Cuatro lecturas honestas:

**1. Siete de nueve combinaciones perdieron dinero.** Ese es el resultado normal.
Si esperabas que cualquier bot bien programado ganara, esta tabla es la respuesta
más útil de todo el repositorio.

**2. El kill switch funcionó exactamente como se configuró.** Las tres peores
combinaciones se detuvieron solas cerca del 15% de drawdown, en vez de seguir
perdiendo. Comparado con el −100% que permite un bot sin límites, esa es toda la
diferencia. **Los límites son el producto; la estrategia es un detalle.**

**3. Las temporalidades cortas fueron las peores, y no por casualidad.** En 15m se
opera el doble y se gana menos. Con 0,1% de comisión por lado, cada operación
empieza ~0,2% en contra sobre un stop del 1,2%: pagas el 17% de tu riesgo en peaje
antes de que el precio se mueva. Por eso el intervalo por defecto es `1h`, no `5m`.

**4. Lo que ganó aquí puede perder mañana.** `mean_reversion` en 1h ganó porque
estos 90 días fueron laterales. En una tendencia sostenida, esa misma estrategia
compra caídas todo el camino hacia abajo. Elegir la fila ganadora de una tabla es
sobreajuste, no análisis. Compruébalo en varios periodos distintos antes de creerte
nada.

```bash
# comparar solo dos estrategias en un periodo distinto
python3 -m zzbot compare --days 180 --timeframes 1h,4h --strategies mean_reversion,breakout
```

---

## Arquitectura

```
zzbot/
├── config.py        Configuración validada (rechaza combinaciones incoherentes)
├── models.py        Candle, Signal, Position, Trade
├── indicators.py    EMA, RSI, ATR, ADX, Bollinger, MACD, z-score (sin dependencias)
├── risk.py          <<< GESTOR DE RIESGO: las tres capas de frenos
├── portfolio.py     Contabilidad: efectivo, posiciones, comisiones, PnL
├── scanner.py       Selección de universo y escaneo paralelo
├── engine.py        Bucle principal
├── backtest.py      Replay histórico con la misma lógica
├── storage.py       Diario en SQLite (sobrevive a reinicios)
├── notify.py        Webhook / Telegram opcional
├── exchanges/       Datos de mercado (Binance público, sin API key)
└── strategies/      trend_momentum, mean_reversion, breakout
```

Orden de cada ciclo, deliberadamente así:

1. Refrescar precios y equity
2. **Gestionar lo abierto** (stops, take profit, trailing, tiempo)
3. Comprobar límites globales y diarios
4. Solo entonces, buscar nuevas entradas

Primero se protege lo que ya está arriesgado, después se busca más riesgo. Al revés,
un ciclo lento podría abrir posiciones nuevas mientras una vieja se desangra.

---

## Dejarlo corriendo

Hay una config lista para esto: `config.paper.yaml` (mean_reversion en 1h, la
combinación que mejor aguantó en la comparativa, con límites más estrechos que
los del default: pérdida diaria 2%, drawdown máximo 10%).

```bash
python3 -m zzbot -c config.paper.yaml run
```

Para que sobreviva a cerrar la terminal:

```bash
setsid nohup python3 -u -m zzbot -c config.paper.yaml run >> paper_1h.log 2>&1 < /dev/null &
tail -f paper_1h.log            # seguir la actividad
python3 -m zzbot -c config.paper.yaml status   # estado y operaciones
```

**No lo lances con `| tee`.** Es tentador para ver la salida y guardarla a la vez,
pero encadena el bot a un proceso que no controlas: si el `tee` muere (cierras la
terminal, se reinicia la máquina, se cae la sesión), el bot queda escribiendo a una
tubería sin lector y pierde todo el registro, o muere en el siguiente log. `setsid`
más redirección directa al archivo no depende de nadie: comprobado a base de
perder doce horas de registro así.

Como servicio, para que arranque solo tras un reinicio:

```ini
# /etc/systemd/system/zzbot.service
[Unit]
Description=ZZBOT paper trading
After=network-online.target

[Service]
Type=simple
User=TU_USUARIO
WorkingDirectory=/ruta/a/ZZBOT
ExecStart=/usr/bin/python3 -u -m zzbot -c config.paper.yaml run
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now zzbot
journalctl -u zzbot -f
```

`Restart=on-failure` reinicia si el proceso muere por un error, pero **no**
cuando el bot se detiene solo por el kill switch: esa parada es una decisión,
no un fallo, y reiniciarla automáticamente anularía el límite que la provocó.

Dos cosas que conviene saber antes de dejarlo días:

- **Con `mean_reversion` en 1h van a pasar horas sin ninguna operación.** Es lo
  normal: en la prueba de 90 días hizo 44 operaciones sobre 8 mercados, menos de
  una al día. Un bot que opera poco no está roto; uno que opera constantemente
  suele estar pagando comisiones por ruido.
- **Revísalo cada pocos días, no cada hora.** Lo que importa es el resumen de
  `status`, no cada operación suelta.

---

## Persistencia

El estado vive en SQLite (`zzbot_state.sqlite`): operaciones cerradas, posiciones
abiertas, curva de equity y estado del gestor de riesgo. Si reinicias el bot a
media sesión, **los límites del día siguen contando**. Un bot que olvida que ya
perdió el 3% hoy no tiene límite diario.

---

## Modo live

El motor y los límites están completos, pero **el broker con dinero real no viene
implementado**, y es a propósito. Para operar en real necesitas:

1. Implementar un `Broker` (ver `zzbot/exchanges/base.py`) con las llamadas
   firmadas del exchange: crear orden, consultar orden, cancelar.
2. Probarlo **primero en el testnet** del exchange.
3. Poner `mode: live` **y** `dry_run_confirm: true` en la config (el doble seguro
   es intencional).

Las claves de API se leen de variables de entorno, nunca del archivo de config:

```bash
export ZZBOT_API_KEY=...
export ZZBOT_API_SECRET=...
```

Recomendaciones al crear la clave: permisos **solo de spot trading**, retiros
**desactivados**, y restricción por IP.

---

## Tests

```bash
python3 -m unittest discover tests -v
```

La mayoría cubren el gestor de riesgo, que es donde un fallo cuesta dinero:
dimensionamiento, límites diarios, kill switch, trailing que nunca afloja, y el
caso en que una vela toca stop y take profit a la vez.

---

## Advertencia

Operar con criptomonedas conlleva riesgo real de pérdida total. Este software se
entrega sin garantías, como herramienta educativa y de investigación. Los
resultados pasados no predicen resultados futuros. No es asesoramiento financiero.

Si decides usarlo con dinero real: empieza con una cantidad que puedas perder por
completo sin que cambie nada en tu vida, y déjalo semanas en paper antes.
