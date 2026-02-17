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
        info = self.levels.level_info()

        # Stop-loss: just beyond the swept level (the liquidity zone edge)
        atr_buf = info.atr * self.levels.config.atr_buffer_mult
        if side == "buy":
            stop_loss = info.nearest_low - atr_buf
        else:
            stop_loss = info.nearest_high + atr_buf

        return TradeSignal(
            side=side,
            strength=strength,
            reason=f"liquidity_sweep nOFI={m.nofi:+.4f}",
            price=self._last_price,
            stop_loss=round(stop_loss, 2),
            timestamp_ms=time.time() * 1000,
        )

    async def execute(self, order: Any) -> None:
        # TODO: place orders via exchange client
        logger.info(
            f"[EXEC] {order.side.upper()} {order.position_size:.6f} "
            f"@ {order.entry_price:.2f} | SL={order.stop_loss:.2f} "
            f"TP={order.take_profit:.2f} | {order.reason}"
        )
