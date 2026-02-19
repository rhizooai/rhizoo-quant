# Rhizoo Alpha Bot

A persistent, event-driven trading engine that detects **liquidity sweep** patterns across multi-timeframe levels, confirmed by real-time order flow imbalance. Includes a macro-context regime filter (EMA 200 + ADX) and a full risk management pipeline with circuit breakers.

## How It Works

The bot connects to an exchange via WebSocket and continuously streams live trades. Each tick batch flows through a pipeline:

```
WebSocket Trade Stream
  |
  v
ImbalanceTracker          ← nOFI, volume Z-Score, absorption detection
  |
  v
LevelTracker              ← 1-min candle synthesis, H1/H4 liquidity levels
  |
  v
LiquiditySweepStrategy    ← Hunter state machine (SCANNING → SWEEPING → CONFIRMING)
  |                          + MarketRegime filter (EMA 200 1H, ADX, 15m divergence)
  v
RiskManager               ← Position sizing, spread guard, circuit breakers
  |
  v
Execution                 ← Paper broker (simulated) or live exchange
```

### Strategy: Liquidity Sweep Hunter

The core strategy identifies **stop-hunt sweeps** — price spikes beyond key liquidity levels (H1/H4 highs and lows) that reverse with strong order flow imbalance:

1. **SCANNING** — Monitors price distance to H1/H4 extremes.
2. **SWEEPING** — Price pierces a level. Tracks the wick extreme (furthest point beyond the level).
3. **CONFIRMING** — Price snaps back inside the level. Waits for nOFI confirmation (strong opposing flow).
4. **SIGNAL** — Confirmed sweep emits a trade signal with entry, stop-loss (wick extreme), and take-profit (Fibonacci 0.5 of the range).

### Market Regime Filter

Before any signal reaches the risk manager, a macro-context layer checks alignment with the broader trend:

- **EMA 200 (1H)** — Determines macro trend direction (BULLISH / BEARISH).
- **ADX (14-period)** — Measures trend strength. ADX > 50 = extreme (crash/melt-up protection).
- **EMA 50 (15m)** — Allows counter-trend signals when the shorter timeframe shows divergence.

| Condition | Buy Signals | Sell Signals |
|-----------|------------|--------------|
| Bullish trend (price > EMA 200) | Allowed | Allowed only on 15m bearish divergence |
| Bearish trend (price < EMA 200) | Allowed only on 15m bullish divergence | Allowed |
| Extreme ADX + opposing trend | **Blocked** | **Blocked** |

The regime refreshes every 15 minutes via background task.

### Risk Management

The `RiskManager` acts as a gatekeeper before any order is placed:

- **Position sizing** — Risk-based: `(Balance * 1%) / |Entry - StopLoss|`
- **Daily loss circuit breaker** — Halts all trading if cumulative daily loss exceeds 3%.
- **Consecutive loss limit** — Pauses after 3 consecutive losses.
- **Volatility circuit breaker** — Halts signals when volume Z-Score exceeds 4.0.
- **Spread guard** — Rejects signals if bid/ask spread > 0.1%.

## Quick Start

```bash
# From the repo root
cp .env.example .env
# Edit .env with your API credentials

# Navigate to the bot
cd bots/rhizoo-alpha-bot

# Set up the virtual environment + install dependencies
bash setup.sh

# Activate the environment
source .venv/bin/activate

# Run (defaults to BTC/USDT)
python main.py
```

### CLI Options

```bash
# Hunt a specific pair
python main.py -s ETH/USDT

# Lowercase is auto-normalized
python main.py --symbol sol/usdt
```

If the symbol is invalid, the bot logs an error with suggestions and exits cleanly.

## Paper Trading

Paper trading lets you validate the strategy with simulated execution before risking real capital. It uses the same signal pipeline and risk management — the only difference is orders are filled virtually instead of hitting the exchange.

### Enabling Paper Trading

Set `PAPER_TRADING=true` in your `.env` (root or bot-level):

```env
PAPER_TRADING=true
PAPER_BALANCE=10000.0
```

### What It Does

- Simulates fills with **0.05% commission** per side (entry and exit).
- Monitors all open positions tick-by-tick for stop-loss and take-profit hits.
- Closes at the SL/TP price (not the tick price) for realistic limit-order simulation.
- Tracks win rate, profit factor, max drawdown, and net PnL.
- Logs every entry and exit as a formatted ticket in the console.

### Trade Logs

Each paper trade is appended to a CSV file in `logs/`:

```
logs/simulated_trades_BTC_USDT.csv
logs/simulated_trades_ETH_USDT.csv
```

The filename includes the symbol so running multiple pairs simultaneously won't overwrite data.

CSV columns: `id, timestamp, pair, side, entry, sl, tp, size, exit_price, pnl, result`

### Dashboard Output

While running, the bot prints a pulse every 5 seconds:

```
==================================================
  RHIZOO ALPHA BOT | ACTIVE PAIR: BTC/USDT
==================================================
Trend:            BEARISH
nOFI:             -0.4523 (Moderate Sell Bias)
Volume Intensity: 1.8 sigma (ELEVATED)
...

--- RHIZOO ALPHA CONTEXT ---
Macro Trend (1H): BEARISH (Price below EMA 200)
Trend Strength:   STRONG (ADX: 32)
EMA 200 (1H):    96,450.00
Action:           ONLY TAKING SHORT SWEEPS (Bullish Sweeps Filtered)

--- PAPER TRADING STATS ---
Balance:          10,045.20
Net PnL:          +45.20
Win Rate:         66.7%
...
```

## Telemetry (Redis Pub/Sub)

The bot includes an optional telemetry bridge that broadcasts real-time events to a Redis Pub/Sub channel (`rhizoo_telemetry`). External services — dashboards, alerting systems, or custom UIs — can subscribe to receive live state.

### Event Types

| Event | Trigger | Payload |
|-------|---------|---------|
| `MARKET_PULSE` | Every 5s pulse | symbol, nOFI, volume Z-Score, efficiency, trend, price, ATR |
| `LEVEL_UPDATE` | Every 5s pulse | H1/H4 highs and lows, near liquidity, hunt summary |
| `SIGNAL_GEN` | Strategy finds a setup | side, strength, price, SL, TP, reason |
| `TRADE_UPDATE` | Paper entry or exit | action (ENTRY/EXIT), id, pair, side, prices, PnL |

### Setup

Telemetry is **fire-and-forget** — if Redis is not available, the bot logs a warning and continues trading normally.

**Local Redis:**

```bash
# Ubuntu/Debian
sudo apt install redis-server && redis-server

# macOS
brew install redis && redis-server
```

**Hosted Redis (Upstash, Redis Cloud, etc.):**

```env
REDIS_HOST=your-host.upstash.io
REDIS_PORT=6380
REDIS_PASSWORD=your_auth_token
REDIS_SSL=true
```

### Testing with the Mock Listener

A test script is included to verify the telemetry pipeline:

```bash
# Terminal 1: run the bot
source .venv/bin/activate
python main.py

# Terminal 2: subscribe to events
source .venv/bin/activate
python services/ui_mock.py
```

The mock listener connects to the same Redis instance and prints color-coded JSON events as they arrive in real time.

## Watch Window (Real-Time Dashboard)

A browser-based charting dashboard that visualizes the bot's live state via TradingView Lightweight Charts. Connects to the same Redis telemetry stream and renders candlestick charts, trade markers, liquidity levels, and order flow metrics in real time.

### Running the Dashboard

```bash
# Install dashboard dependencies (one time)
pip install -r services/dashboard/requirements.txt

# Start the dashboard server
uvicorn services.dashboard.main:app --host 0.0.0.0 --port 8080
```

Open `http://localhost:8080` in your browser. The dashboard auto-connects to `BTC/USDT` — type a different symbol and click **Connect** to switch.

### What It Shows

- **Candlestick chart** — 1-minute candles built from live price ticks
- **Trade markers** — Green arrows for buy entries, red arrows for sell entries, circles for exits
- **H1/H4 liquidity levels** — Dashed price lines (orange for H4, blue for H1)
- **Overlay HUD** — Price, nOFI (color-coded), trend direction, volume Z-Score, hunt status
- **Event log** — Scrollable feed of signals and trade events with timestamps

### Requirements

The dashboard requires:
1. **Redis running** — the bot must be broadcasting telemetry events
2. **Bot running** — start the bot in a separate terminal (`python main.py`)
3. Same `REDIS_*` environment variables as the bot

## Project Structure

```
rhizoo-alpha-bot/
├── main.py                  # Entry point, event loop, dashboard, regime wiring
├── requirements.txt
├── setup.sh                 # One-command environment setup
├── core/
│   ├── exchange_client.py   # ccxt.pro WebSocket client + REST methods
│   ├── risk_manager.py      # Gatekeeper: sizing, circuit breakers, spread guard
│   ├── paper_broker.py      # Virtual execution engine + CSV logging
│   ├── telemetry.py         # Redis Pub/Sub event broadcaster
│   └── logger.py            # Loguru configuration (console + file rotation)
├── data/
│   └── processor.py         # ImbalanceTracker, LevelTracker, MarketRegime
├── strategies/
│   ├── base_strategy.py     # Abstract strategy interface
│   └── liquidity_sweep.py   # Liquidity Sweep Hunter implementation
├── services/
│   ├── ui_mock.py           # Mock telemetry listener (test tool)
│   └── dashboard/           # Real-time Watch Window
│       ├── main.py          # FastAPI backend (Redis subscriber + WebSocket fan-out)
│       ├── requirements.txt # Dashboard-specific dependencies
│       └── static/
│           └── index.html   # TradingView Lightweight Charts frontend
└── logs/
    ├── alpha.log             # Rotating log file (10 MB, 7-day retention)
    └── simulated_trades_*.csv
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `BINANCE_API_KEY` | — | Binance API key (set in root `.env`) |
| `BINANCE_SECRET` | — | Binance API secret (set in root `.env`) |
| `ACCOUNT_BALANCE` | `10000.0` | Account equity for position sizing (USDT) |
| `ZSCORE_THRESHOLD` | `2.0` | Volume Z-Score threshold for signal detection |
| `PAPER_TRADING` | `false` | Enable simulated execution (`true` / `false`) |
| `PAPER_BALANCE` | `10000.0` | Virtual starting balance for paper trades |
| `LOG_LEVEL` | `DEBUG` | Logging verbosity |
| `REDIS_HOST` | `localhost` | Redis hostname for telemetry |
| `REDIS_PORT` | `6379` | Redis port |
| `REDIS_PASSWORD` | — | Redis auth password (leave empty for local) |
| `REDIS_SSL` | `false` | Enable TLS for hosted Redis (`true` / `false`) |

See the root [`.env.example`](../../.env.example) for the full configuration reference and instructions on the two-layer `.env` system.

## Shutdown

The bot handles `SIGINT` (Ctrl+C) and `SIGTERM` gracefully:

1. Cancels the WebSocket stream and regime refresh task.
2. Prints final paper trading stats (if enabled).
3. Closes the exchange connection.

No data is lost — all paper trades are written to CSV immediately on close.
