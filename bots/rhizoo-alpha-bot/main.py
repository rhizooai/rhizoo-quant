"""Rhizoo Alpha Bot — persistent event-driven trading engine."""

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
import signal

from core.logger import logger
from core.exchange_client import ExchangeClient, ExchangeConfig
from core.risk_manager import RiskManager
from data.processor import DataBuffer
from strategies.liquidity_sweep import LiquiditySweepStrategy


async def run() -> None:
    """Long-running coroutine: streams trades and drives strategy evaluation."""
    symbol = "BTC/USDT"

    client = ExchangeClient(ExchangeConfig(sandbox=True))
    risk = RiskManager()
    buffer = DataBuffer()
    strategy = LiquiditySweepStrategy()

    logger.info(f"Rhizoo Alpha Bot starting — streaming {symbol}")

    try:
        async for trades in client.stream_trades(symbol):
            buffer.push(trades)
            logger.debug(f"Buffer: {buffer.size} trades (batch of {len(trades)})")

            for trade in trades:
                await strategy.on_data(trade)

            signal_result = await strategy.generate_signal()
            if signal_result:
                market = trades[-1] if trades else {}
                if await risk.evaluate(signal_result, market):
                    logger.info(f"Signal approved: {signal_result}")
                    await strategy.execute(signal_result)
                else:
                    logger.warning(f"Signal rejected by RiskManager: {signal_result}")

    except asyncio.CancelledError:
        logger.info("Run loop cancelled — shutting down")
    finally:
        await client.close()


def main() -> None:
    """Entry point: sets up the event loop with graceful shutdown."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    task = loop.create_task(run())

    def _shutdown(sig: signal.Signals) -> None:
        logger.info(f"Received {sig.name} — initiating graceful shutdown")
        task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown, sig)

    try:
        loop.run_until_complete(task)
    except asyncio.CancelledError:
        pass
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
        logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
