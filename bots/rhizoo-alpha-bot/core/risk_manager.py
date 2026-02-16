from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from core.logger import logger


class RiskConfig(BaseModel):
    max_position_size: float = Field(default=1.0, description="Max position size in base currency")
    max_drawdown_pct: float = Field(default=0.05, description="Max drawdown before halting (5%)")


class RiskManager:
    """Risk management gate — currently a pass-through.

    Initialized in the event loop to enforce risk rules before order execution.
    """

    def __init__(self, config: RiskConfig | None = None) -> None:
        self.config = config or RiskConfig()
        logger.info(f"RiskManager initialized (max_position={self.config.max_position_size})")

    async def evaluate(self, signal: str, market_data: dict[str, Any]) -> bool:
        """Return True if the signal passes risk checks.

        Currently a pass-through — always approves.
        """
        # TODO: implement position sizing, drawdown checks, exposure limits
        return True
