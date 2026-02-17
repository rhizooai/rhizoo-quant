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
import time

from core.logger import logger
from core.exchange_client import ExchangeClient, ExchangeConfig
from core.risk_manager import RiskManager
from data.processor import ImbalanceTracker, LevelInfo, MarketMetrics
from strategies.liquidity_sweep import LiquiditySweepStrategy

PULSE_INTERVAL_SEC = 5.0


def _nofi_label(nofi: float) -> str:
    abs_n = abs(nofi)
    direction = "Buy" if nofi > 0 else "Sell"
    if abs_n >= 0.7:
        return f"Strong {direction} Bias"
    if abs_n >= 0.3:
        return f"Moderate {direction} Bias"
    return "Balanced"


def _vol_label(zscore: float) -> str:
    if zscore >= 3.0:
        return "EXTREME"
    if zscore >= 2.0:
        return "HEAVY"
    if zscore >= 1.0:
        return "ELEVATED"
    return "NORMAL"


def _eff_label(efficiency: float) -> str:
    abs_e = abs(efficiency)
    if abs_e >= 0.01:
        return "Clear Path"
    if abs_e >= 0.001:
        return "Moderate"
    return "Stalled / Absorbed"


def _print_pulse(m: MarketMetrics, lv: LevelInfo) -> None:
    logger.info(
        f"\n--- RHIZOO ALPHA PULSE ---\n"
        f"Trend:            {m.trend}\n"
        f"nOFI:             {m.nofi:+.4f} ({_nofi_label(m.nofi)})\n"
        f"Volume Intensity: {m.volume_zscore:.1f} sigma ({_vol_label(m.volume_zscore)})\n"
        f"Efficiency:       {m.efficiency:+.6f} ({_eff_label(m.efficiency)})\n"
        f"Absorption:       {'YES' if m.is_absorption else 'NO'}\n"
        f"Nearest High:     {lv.nearest_high:.1f} (Dist: {lv.high_distance_pct:.1f}%)\n"
        f"Nearest Low:      {lv.nearest_low:.1f} (Dist: {lv.low_distance_pct:.1f}%)\n"
        f"ATR:              {lv.atr:.2f}\n"
        f"Sweep Status:     {lv.sweep_status}\n"
        f"Status:           {m.status}\n"
        f"--------------------------"
    )


async def run() -> None:
    """Long-running coroutine: streams trades, computes metrics, drives strategy."""
    symbol = "BTC/USDT"

    client = ExchangeClient(ExchangeConfig(sandbox=True))
    risk = RiskManager()
    tracker = ImbalanceTracker()
    strategy = LiquiditySweepStrategy()

    logger.info(f"Rhizoo Alpha Bot starting — streaming {symbol}")

    last_pulse = 0.0

    try:
        async for trades in client.stream_trades(symbol):
            tracker.push(trades)

            metrics = tracker.compute_metrics()
            risk.update_metrics(metrics)

            for trade in trades:
                await strategy.on_data(trade)

            signal_result = await strategy.generate_signal(metrics)
            if signal_result:
                market = trades[-1] if trades else {}
                if await risk.evaluate(signal_result, market):
                    logger.info(f"Signal approved: {signal_result.side.upper()} strength={signal_result.strength}")
                    await strategy.execute(signal_result)
                else:
                    logger.warning(f"Signal rejected by RiskManager: {signal_result.side}")

            # Pulse dashboard — every 5 seconds when market is not idle
            now = time.monotonic()
            if now - last_pulse >= PULSE_INTERVAL_SEC and tracker.size > 0:
                level_info = strategy.levels.level_info()
                _print_pulse(metrics, level_info)
                last_pulse = now

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
