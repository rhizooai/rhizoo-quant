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

import argparse
import asyncio
import os
import signal
import time

from core.logger import logger
from core.exchange_client import ExchangeClient, ExchangeConfig
from core.risk_manager import RiskManager
from core.telemetry import TelemetryClient
from data.processor import ImbalanceTracker, LevelInfo, MarketMetrics, MarketRegime
from strategies.liquidity_sweep import LiquiditySweepStrategy

PULSE_INTERVAL_SEC = 5.0

PAPER_TRADING = os.getenv("PAPER_TRADING", "false").lower() == "true"
if PAPER_TRADING:
    from core.paper_broker import PaperBroker
    from data.processor import PositionMonitor


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


def _print_pulse(m: MarketMetrics, lv: LevelInfo, symbol: str = "BTC/USDT") -> None:
    header = f"RHIZOO ALPHA BOT | ACTIVE PAIR: {symbol}"
    border = "=" * max(len(header) + 4, 50)
    logger.info(
        f"\n{border}\n  {header}\n{border}\n"
        f"Trend:            {m.trend}\n"
        f"nOFI:             {m.nofi:+.4f} ({_nofi_label(m.nofi)})\n"
        f"Volume Intensity: {m.volume_zscore:.1f} sigma ({_vol_label(m.volume_zscore)})\n"
        f"Efficiency:       {m.efficiency:+.6f} ({_eff_label(m.efficiency)})\n"
        f"Absorption:       {'YES' if m.is_absorption else 'NO'}\n"
        f"H4 High:          {lv.h4_high:.1f} ({lv.h4_high_dist_pct:+.3f}%)\n"
        f"H4 Low:           {lv.h4_low:.1f} ({lv.h4_low_dist_pct:+.3f}%)\n"
        f"H1 High:          {lv.h1_high:.1f} ({lv.h1_high_dist_pct:+.3f}%)\n"
        f"H1 Low:           {lv.h1_low:.1f} ({lv.h1_low_dist_pct:+.3f}%)\n"
        f"ATR:              {lv.atr:.2f}\n"
        f"Near Liquidity:   {lv.near_liquidity}\n"
        f"Sweep Status:     {lv.hunt_summary}\n"
        f"Status:           {m.status}\n"
        f"--------------------------"
    )


def _print_entry_ticket(pos) -> None:
    logger.info(
        f"\n"
        f"╔══════════════════════════════════════╗\n"
        f"║   SIMULATED TRADE — ENTRY            ║\n"
        f"╠══════════════════════════════════════╣\n"
        f"║  ID:     {pos.id:<28}║\n"
        f"║  Side:   {pos.side.upper():<28}║\n"
        f"║  Entry:  {pos.entry_price:<28.2f}║\n"
        f"║  SL:     {pos.stop_loss:<28.2f}║\n"
        f"║  TP:     {pos.take_profit:<28.2f}║\n"
        f"║  Size:   {pos.position_size:<28.6f}║\n"
        f"║  Reason: {pos.reason:<28}║\n"
        f"╚══════════════════════════════════════╝"
    )


def _print_exit_ticket(trade) -> None:
    pnl_str = f"{trade.pnl:+.2f}"
    logger.info(
        f"\n"
        f"╔══════════════════════════════════════╗\n"
        f"║   SIMULATED TRADE — EXIT             ║\n"
        f"╠══════════════════════════════════════╣\n"
        f"║  ID:     {trade.id:<28}║\n"
        f"║  Side:   {trade.side.upper():<28}║\n"
        f"║  Entry:  {trade.entry_price:<28.2f}║\n"
        f"║  Exit:   {trade.exit_price:<28.2f}║\n"
        f"║  PnL:    {pnl_str:<28}║\n"
        f"║  Result: {trade.result:<28}║\n"
        f"╚══════════════════════════════════════╝"
    )


def _print_paper_stats(stats: dict) -> None:
    pf = f"{stats['profit_factor']:.2f}" if stats["profit_factor"] != float("inf") else "INF"
    logger.info(
        f"\n--- PAPER TRADING STATS ---\n"
        f"Balance:          {stats['virtual_balance']:.2f}\n"
        f"Net PnL:          {stats['net_pnl']:+.2f}\n"
        f"Win Rate:         {stats['win_rate_pct']:.1f}%\n"
        f"Profit Factor:    {pf}\n"
        f"Max Drawdown:     {stats['max_drawdown_pct']:.2f}%\n"
        f"Total Trades:     {stats['total_trades']}\n"
        f"Active Positions: {stats['active_positions']}\n"
        f"---------------------------"
    )


def _print_macro_context(regime: MarketRegime) -> None:
    if not regime.ready:
        return
    trend = regime.trend_1h
    if trend == "BULLISH":
        trend_detail = "Price above EMA 200"
    elif trend == "BEARISH":
        trend_detail = "Price below EMA 200"
    else:
        trend_detail = "EMA not computed"
    logger.info(
        f"\n--- RHIZOO ALPHA CONTEXT ---\n"
        f"Macro Trend (1H): {trend} ({trend_detail})\n"
        f"Trend Strength:   {regime.trend_strength} (ADX: {regime.adx_1h:.0f})\n"
        f"EMA 200 (1H):    {regime.ema_200_1h:,.2f}\n"
        f"Action:           {regime.action_label}\n"
        f"-----------------------------"
    )


async def _refresh_regime(
    client: ExchangeClient, regime: MarketRegime, symbol: str
) -> None:
    """Refresh macro context every 15 minutes."""
    while True:
        await asyncio.sleep(15 * 60)
        try:
            ohlcv_1h = await client.fetch_ohlcv(symbol, "1h", limit=200)
            ohlcv_15m = await client.fetch_ohlcv(symbol, "15m", limit=200)
            regime.update(ohlcv_1h, ohlcv_15m)
            logger.info(
                f"[REGIME] Refreshed — Trend: {regime.trend_1h}, "
                f"ADX: {regime.adx_1h:.1f}"
            )
        except Exception as exc:
            logger.warning(f"[REGIME] Refresh failed: {exc} — using stale data")


async def run(symbol: str) -> None:
    """Long-running coroutine: streams trades, computes metrics, drives strategy."""
    client = ExchangeClient(ExchangeConfig(sandbox=True))
    telemetry = TelemetryClient()
    refresh_task = None
    paper_broker = None

    try:
        await telemetry.connect()
        symbol = await client.validate_symbol(symbol)
        risk = RiskManager()
        tracker = ImbalanceTracker()
        strategy = LiquiditySweepStrategy()

        # Market regime — macro trend filter
        regime = MarketRegime()
        strategy.regime = regime

        try:
            ohlcv_1h = await client.fetch_ohlcv(symbol, "1h", limit=200)
            ohlcv_15m = await client.fetch_ohlcv(symbol, "15m", limit=200)
            regime.load(ohlcv_1h, ohlcv_15m)
            logger.info(
                f"Market regime loaded — Trend: {regime.trend_1h}, "
                f"ADX: {regime.adx_1h:.1f}, EMA200(1H): {regime.ema_200_1h:.2f}"
            )
            refresh_task = asyncio.create_task(_refresh_regime(client, regime, symbol))
        except Exception as exc:
            logger.warning(f"Failed to load market regime: {exc} — running without macro filter")

        position_monitor = None
        if PAPER_TRADING:
            paper_broker = PaperBroker(pair=symbol)
            position_monitor = PositionMonitor(paper_broker)
            logger.info("PAPER TRADING MODE ACTIVE")

        logger.info(f"Rhizoo Alpha Bot starting — streaming {symbol}")

        last_pulse = 0.0

        async for trades in client.stream_trades(symbol):
            tracker.push(trades)

            metrics = tracker.compute_metrics()
            risk.update_metrics(metrics)

            for trade in trades:
                await strategy.on_data(trade)

            # Paper trading: check SL/TP before next signal generation
            if PAPER_TRADING:
                last_price = trades[-1]["price"]
                closed = position_monitor.check_positions(last_price)
                for ct in closed:
                    _print_exit_ticket(ct)
                    risk.record_fill(ct.pnl)
                    await telemetry.broadcast_event("TRADE_UPDATE", {
                        "action": "EXIT",
                        "id": ct.id,
                        "pair": ct.pair,
                        "side": ct.side,
                        "entry_price": ct.entry_price,
                        "exit_price": ct.exit_price,
                        "pnl": ct.pnl,
                        "result": ct.result,
                    })

            # Strategy → Signal → Regime Override → RiskManager → ValidatedOrder
            trade_signal = await strategy.generate_signal(metrics)

            if trade_signal:
                await telemetry.broadcast_event("SIGNAL_GEN", {
                    "side": trade_signal.side,
                    "strength": trade_signal.strength,
                    "price": trade_signal.price,
                    "stop_loss": trade_signal.stop_loss,
                    "take_profit": trade_signal.take_profit,
                    "reason": trade_signal.reason,
                })

            # Second gate: extreme-trend override (catches edge cases)
            if trade_signal and regime.ready and regime.is_extreme_trend:
                if not regime.is_signal_allowed(trade_signal.side):
                    logger.warning(
                        f"[REGIME] RiskManager override: extreme ADX ({regime.adx_1h:.0f}) "
                        f"blocks {trade_signal.side.upper()} signal"
                    )
                    trade_signal = None

            if trade_signal:
                bid, ask = await client.get_bid_ask(symbol)
                order = risk.process_signal(trade_signal, bid, ask)
                if order:
                    if PAPER_TRADING:
                        if paper_broker.active_positions:
                            logger.debug("Paper position already active — skipping new entry")
                        else:
                            position = paper_broker.execute_order(order)
                            _print_entry_ticket(position)
                            await telemetry.broadcast_event("TRADE_UPDATE", {
                                "action": "ENTRY",
                                "id": position.id,
                                "pair": position.pair,
                                "side": position.side,
                                "entry_price": position.entry_price,
                                "stop_loss": position.stop_loss,
                                "take_profit": position.take_profit,
                                "position_size": position.position_size,
                            })
                    else:
                        await strategy.execute(order)
                else:
                    logger.debug(f"Signal filtered by RiskManager: {trade_signal.side}")

            # Pulse dashboard — every 5 seconds when market is not idle
            now = time.monotonic()
            if now - last_pulse >= PULSE_INTERVAL_SEC and tracker.size > 0:
                level_info = strategy.levels.level_info()
                _print_pulse(metrics, level_info, symbol)
                _print_macro_context(regime)
                if PAPER_TRADING:
                    _print_paper_stats(paper_broker.get_stats())
                last_pulse = now

                # Telemetry broadcasts
                await telemetry.broadcast_event("MARKET_PULSE", {
                    "symbol": symbol,
                    "nofi": metrics.nofi,
                    "volume_zscore": metrics.volume_zscore,
                    "efficiency": metrics.efficiency,
                    "trend": metrics.trend,
                    "is_absorption": metrics.is_absorption,
                    "price": level_info.current_price,
                    "atr": level_info.atr,
                })
                await telemetry.broadcast_event("LEVEL_UPDATE", {
                    "symbol": symbol,
                    "h1_high": level_info.h1_high,
                    "h1_low": level_info.h1_low,
                    "h4_high": level_info.h4_high,
                    "h4_low": level_info.h4_low,
                    "near_liquidity": level_info.near_liquidity,
                    "hunt_summary": level_info.hunt_summary,
                })

    except asyncio.CancelledError:
        logger.info("Run loop cancelled — shutting down")
    finally:
        if refresh_task is not None:
            refresh_task.cancel()
            try:
                await refresh_task
            except asyncio.CancelledError:
                pass
        if PAPER_TRADING and paper_broker is not None:
            logger.info("=== FINAL PAPER TRADING STATS ===")
            _print_paper_stats(paper_broker.get_stats())
        await telemetry.close()
        await client.close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rhizoo Alpha Bot — liquidity sweep hunter")
    parser.add_argument(
        "-s", "--symbol",
        default="BTC/USDT",
        help="The trading pair to hunt (e.g., ETH/USDT, SOL/USDT).",
    )
    return parser.parse_args()


def main() -> None:
    """Entry point: sets up the event loop with graceful shutdown."""
    args = _parse_args()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    task = loop.create_task(run(args.symbol))

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
