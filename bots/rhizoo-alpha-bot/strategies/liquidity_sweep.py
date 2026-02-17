from __future__ import annotations

import time
from typing import Any

from pydantic import Field

from core.logger import logger
from data.processor import LevelTracker, LevelConfig, MarketMetrics, SweepResult
from strategies.base_strategy import BaseStrategy, StrategyConfig, TradeSignal


class LiquiditySweepConfig(StrategyConfig):
    name: str = Field(default="liquidity_sweep")
    level_config: LevelConfig = Field(default_factory=LevelConfig)


class LiquiditySweepStrategy(BaseStrategy):
    """Liquidity Sweep Hunter — detects stop-hunt sweeps across H1/H4 levels,
    confirmed by order flow imbalance.
    """

    def __init__(self, config: LiquiditySweepConfig | None = None) -> None:
        cfg = config or LiquiditySweepConfig()
        super().__init__(cfg)
        self.levels = LevelTracker(cfg.level_config)
        self._last_price: float = 0.0

    async def on_data(self, market_data: dict[str, Any]) -> None:
        """Feed each trade into the LevelTracker for candle synthesis."""
        self.levels.push_trade(market_data)
        self._last_price = float(market_data.get("price", 0.0))

    async def generate_signal(self, metrics: Any) -> TradeSignal | None:
        """Run the Hunter state machine and emit a signal on confirmed sweep."""
        m: MarketMetrics = metrics
        result: SweepResult | None = self.levels.check_hunt(m.nofi)
        if result is None:
            return None

        return TradeSignal(
            side=result.side,
            strength=result.strength,
            reason=f"sweep_{result.level_name} nOFI={m.nofi:+.4f}",
            price=self._last_price,
            stop_loss=result.wick_extreme,
            take_profit=result.fib_tp,
            timestamp_ms=time.time() * 1000,
            metadata={
                "level_name": result.level_name,
                "level_price": result.level_price,
                "wick_extreme": result.wick_extreme,
                "fib_tp": result.fib_tp,
                "range_high": result.range_high,
                "range_low": result.range_low,
            },
        )

    async def execute(self, order: Any) -> None:
        # TODO: place orders via exchange client
        logger.info(
            f"[EXEC] {order.side.upper()} {order.position_size:.6f} "
            f"@ {order.entry_price:.2f} | SL={order.stop_loss:.2f} "
            f"TP={order.take_profit:.2f} | {order.reason}"
        )
