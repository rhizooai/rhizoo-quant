from __future__ import annotations

from typing import Any

from pydantic import Field

from strategies.base_strategy import BaseStrategy, StrategyConfig


class LiquiditySweepConfig(StrategyConfig):
    name: str = Field(default="liquidity_sweep")
    sweep_threshold: float = Field(
        default=0.02, description="Minimum price deviation to detect a sweep"
    )


class LiquiditySweepStrategy(BaseStrategy):
    """Liquidity sweep strategy — scaffold only."""

    def __init__(self, config: LiquiditySweepConfig | None = None) -> None:
        super().__init__(config or LiquiditySweepConfig())

    async def on_data(self, market_data: dict[str, Any]) -> None:
        # TODO: ingest and buffer market data
        pass

    async def generate_signal(self) -> str | None:
        # TODO: detect liquidity sweep pattern
        return None

    async def execute(self, signal: str) -> None:
        # TODO: place orders via exchange client
        pass
