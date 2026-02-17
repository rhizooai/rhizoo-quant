from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from core.logger import logger


class RiskConfig(BaseModel):
    max_position_size: float = Field(default=1.0, description="Max position size in base currency")
    max_drawdown_pct: float = Field(default=0.05, description="Max drawdown before halting (5%)")
    max_volatility_zscore: float = Field(
        default=4.0, description="Circuit-breaker: halt if volume Z-Score exceeds this"
    )


class RiskManager:
    """Risk management gate.

    Receives MarketMetrics from the ImbalanceTracker so it can enforce
    circuit-breaker rules when volatility spikes.
    """

    def __init__(self, config: RiskConfig | None = None) -> None:
        self.config = config or RiskConfig()
        self._halted = False
        logger.info(f"RiskManager initialized (max_position={self.config.max_position_size})")

    def update_metrics(self, metrics: Any) -> None:
        """Ingest latest MarketMetrics for circuit-breaker evaluation."""
        if metrics.volume_zscore >= self.config.max_volatility_zscore:
            if not self._halted:
                logger.warning(
                    f"CIRCUIT BREAKER: volume Z-Score {metrics.volume_zscore} "
                    f">= {self.config.max_volatility_zscore} — halting signals"
                )
            self._halted = True
        else:
            if self._halted:
                logger.info("Circuit breaker released — resuming normal operation")
            self._halted = False

    async def evaluate(self, signal: Any, market_data: dict[str, Any]) -> bool:
        """Return True if the TradeSignal passes risk checks."""
        if self._halted:
            return False
        # TODO: implement position sizing, drawdown checks, exposure limits
        return True
