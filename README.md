# Rhizoo Quant

A decentralized, high-performance quantitative trading ecosystem.

## Vision

Rhizoo Quant is a modular monorepo designed to house independent, high-frequency trading bots. Each bot operates as a self-contained execution unit with its own strategies, risk management, and exchange connectivity — while sharing common infrastructure and conventions across the ecosystem.

## Architecture

This project follows a **monorepo** approach. The `bots/` directory contains independent execution units, each with its own dependencies, configuration, and deployment lifecycle. This allows:

- **Isolation:** Each bot can be developed, tested, and deployed independently.
- **Shared conventions:** Common patterns (logging, exchange clients, risk management) are consistent across bots.
- **Scalability:** New bots can be scaffolded quickly by following the established structure.

```
rhizoo-quant/
├── .env.example
├── .gitignore
├── README.md
└── bots/
    └── <bot-name>/
        ├── main.py
        ├── requirements.txt
        ├── core/          # Exchange clients, risk management, logging
        ├── strategies/    # Trading strategy implementations
        └── data/          # Data processing and math utilities
```

## Active Projects

| Bot | Status | Description |
|-----|--------|-------------|
| `rhizoo-alpha-bot` | Initializing | First trading engine — liquidity sweep strategy |

## Getting Started

1. Clone the repository.
2. Copy `.env.example` to `.env` and fill in your API keys.
3. Navigate to a bot directory (e.g., `bots/rhizoo-alpha-bot/`).
4. Install dependencies: `pip install -r requirements.txt`
5. Run: `python main.py`

## Requirements

- Python 3.11+
