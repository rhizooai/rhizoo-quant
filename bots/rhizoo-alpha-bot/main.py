"""Rhizoo Alpha Bot — entry point."""

from __future__ import annotations

import sys

# --- Activation Guard ---
# Prevent running outside a virtual environment to avoid polluting system Python.
if sys.prefix == sys.base_prefix:
    print(
        "\n[ERROR] Virtual environment is not active.\n"
        "Run the setup script first, then activate:\n"
        "  bash setup.sh && source .venv/bin/activate\n"
    )
    sys.exit(1)

import asyncio

from core.logger import logger
from core.exchange_client import ExchangeClient, ExchangeConfig
from strategies.liquidity_sweep import LiquiditySweepStrategy


async def main() -> None:
    logger.info("Starting Rhizoo Alpha Bot")

    client = ExchangeClient(ExchangeConfig(sandbox=True))
    strategy = LiquiditySweepStrategy()

    try:
        data = await client.get_market_data("BTC/USDT")
        logger.info(f"Market snapshot: {data}")

        await strategy.on_data(data)
        signal = await strategy.generate_signal()

        if signal:
            logger.info(f"Signal: {signal}")
            await strategy.execute(signal)
        else:
            logger.info("No signal generated")
    finally:
        await client.close()
        logger.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
