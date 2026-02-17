from __future__ import annotations

import time
from typing import Any

from pydantic import Field

from core.logger import logger
from data.processor import LevelTracker, LevelConfig, MarketMetrics
from strategies.base_strategy import BaseStrategy, StrategyConfig, TradeSignal


class LiquiditySweepConfig(StrategyConfig):
    name: str = Field(default="liquidity_sweep")
    level_config: LevelConfig = Field(default_factory=LevelConfig)


class LiquiditySweepStrategy(BaseStrategy):
    """Liquidity sweep strategy — detects stop-hunt sweeps confirmed by order flow."""

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
        """Check for a confirmed sweep using nOFI from the ImbalanceTracker."""
        m: MarketMetrics = metrics
        result = self.levels.check_sweep(m.nofi)
        if result is None:
            return None

        side, strength = result
        return TradeSignal(
            side=side,
            strength=strength,
            reason=f"liquidity_sweep nOFI={m.nofi:+.4f}",
            price=self._last_price,
            timestamp_ms=time.time() * 1000,
        )

    async def execute(self, signal: TradeSignal) -> None:
        # TODO: place orders via exchange client
        logger.info(
            f"[EXEC] {signal.side.upper()} @ {signal.price:.2f} "
            f"strength={signal.strength} reason={signal.reason}"
        )
