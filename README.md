# Rhizoo Quant

A modular monorepo for high-performance quantitative trading bots.

## Active Bots

| Bot | Description | Docs |
|-----|-------------|------|
| [rhizoo-alpha-bot](bots/rhizoo-alpha-bot/) | Liquidity sweep hunter with order flow confirmation, macro regime filter, and full risk management | [README](bots/rhizoo-alpha-bot/README.md) |

## Architecture

Each bot lives in `bots/` as a self-contained unit with its own strategies, dependencies, and configuration — while sharing credentials and conventions across the ecosystem.

```
rhizoo-quant/
├── .env.example          # Shared config template (credentials + global settings)
├── .gitignore
├── README.md
└── bots/
    └── rhizoo-alpha-bot/
        ├── main.py           # Entry point + event loop
        ├── README.md         # Bot-specific documentation
        ├── requirements.txt
        ├── setup.sh          # One-command environment setup
        ├── core/             # Exchange clients, risk management, telemetry, logging
        ├── strategies/       # Trading strategy implementations
        ├── data/             # Data processing, indicators, market regime
        ├── services/         # Test tools (telemetry mock listener)
        └── logs/             # Rotating logs + paper trade CSVs
```

## Tech Stack

| Package | Version | Purpose |
|---------|---------|---------|
| [ccxt / ccxt.pro](https://github.com/ccxt/ccxt) | `>=4.0.0` | Exchange connectivity — unified REST + WebSocket API across 100+ exchanges. Powers live trade streaming (`watch_trades`), OHLCV fetching, market validation, and order execution. |
| [NumPy](https://numpy.org/) | `>=1.24.0` | Vectorized math for all indicators — nOFI, ATR, volume Z-Score, EMA, ADX. Keeps per-tick latency under 10ms. |
| [Pydantic](https://docs.pydantic.dev/) | `>=2.0.0` | Data validation and settings management. Every config, metric, signal, and order is a typed Pydantic model. |
| [Loguru](https://github.com/Delgan/loguru) | `>=0.7.0` | Structured logging with colored console output + rotating file logs (10 MB, 7-day retention, gzip compression). |
| [python-dotenv](https://github.com/theskumar/python-dotenv) | `>=1.0.0` | Two-layer `.env` loading — root credentials inherited by all bots, per-bot overrides take priority. |
| [redis-py](https://github.com/redis/redis-py) | `>=5.0.0` | Async Pub/Sub telemetry bridge. Broadcasts real-time events (pulses, signals, trades) to external services. Optional — bot runs without it. |

## Getting Started

### 1. Clone and configure credentials

```bash
git clone <repo-url> && cd rhizoo-quant
cp .env.example .env
```

Edit `.env` and fill in your exchange API keys. See [`.env.example`](.env.example) for a full reference on the two-layer configuration system.

### 2. Set up a bot

```bash
cd bots/rhizoo-alpha-bot
bash setup.sh            # Creates venv + installs dependencies
source .venv/bin/activate
```

### 3. Run in paper trading mode (recommended first step)

Make sure `PAPER_TRADING=true` is set in your `.env` (it is by default), then:

```bash
python main.py                  # Defaults to BTC/USDT
python main.py -s ETH/USDT     # Hunt a different pair
```

The bot will stream live market data and simulate trades without risking real capital. Check `logs/simulated_trades_*.csv` for results.

See the [Alpha Bot README](bots/rhizoo-alpha-bot/README.md) for detailed documentation on the strategy, risk management, market regime filter, and dashboard output.

### 4. Go live

When you're confident in the strategy's performance:

```env
PAPER_TRADING=false
```

Ensure your API keys have trading permissions and appropriate IP restrictions.

## Environment Configuration

The repo uses a **two-layer `.env` system**:

1. **Root `.env`** (this directory) — Shared credentials and global settings. Every bot inherits these automatically via `python-dotenv`'s directory traversal.
2. **Bot-level `.env`** (e.g. `bots/rhizoo-alpha-bot/.env`) — Optional per-bot overrides. Only set what you want to change; everything else falls through to root.

| Variable | Default | Scope | Description |
|----------|---------|-------|-------------|
| `BINANCE_API_KEY` | — | Root | Exchange API key |
| `BINANCE_SECRET` | — | Root | Exchange API secret |
| `LOG_LEVEL` | `DEBUG` | Root | Logging verbosity |
| `ACCOUNT_BALANCE` | `10000.0` | Bot | Account equity for position sizing |
| `ZSCORE_THRESHOLD` | `2.0` | Bot | Volume Z-Score signal threshold |
| `PAPER_TRADING` | `true` | Bot | Enable simulated execution |
| `PAPER_BALANCE` | `10000.0` | Bot | Virtual starting balance |
| `REDIS_HOST` | `localhost` | Root | Redis hostname for telemetry |
| `REDIS_PORT` | `6379` | Root | Redis port |
| `REDIS_PASSWORD` | — | Root | Redis auth password |
| `REDIS_SSL` | `false` | Root | Enable TLS for hosted Redis |

## Execution Flow

```
main.py → run(symbol)
  │
  ├─ ExchangeClient.validate_symbol()    ← Normalize + verify against exchange
  ├─ MarketRegime.load()                 ← Fetch 1H/15m candles, compute EMA 200 + ADX
  ├─ Background: _refresh_regime()       ← Re-fetch every 15 minutes
  │
  └─ WebSocket trade stream loop:
       ├─ ImbalanceTracker.push()        ← nOFI, efficiency, volume Z-Score
       ├─ LevelTracker.push_trade()      ← Candle synthesis, H1/H4 levels, ATR
       ├─ Strategy.generate_signal()     ← Hunter state machine + regime filter
       ├─ RiskManager.process_signal()   ← Sizing, circuit breakers, spread guard
       ├─ Execute (paper or live)
       └─ TelemetryClient.broadcast()  ← Pub/Sub events to Redis (optional)
```

## Requirements

- Python 3.11+
- Exchange API credentials (Binance testnet supported via sandbox mode)
- Redis (optional, for telemetry) — [install locally](https://redis.io/docs/getting-started/) or use a hosted provider like [Upstash](https://upstash.com/), [Redis Cloud](https://redis.io/cloud/), or AWS ElastiCache
