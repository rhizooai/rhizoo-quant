from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field


class StrategyConfig(BaseModel):
    name: str = Field(..., description="Strategy identifier")
    symbol: str = Field(default="BTC/USDT", description="Trading pair")
    timeframe: str = Field(default="5m", description="Candle timeframe")


class TradeSignal(BaseModel):
    """Structured signal emitted by a strategy — consumed by RiskManager."""

    side: str = Field(..., description="'buy' or 'sell'")
    strength: str = Field(default="MEDIUM", description="HIGH / MEDIUM / LOW")
    reason: str = Field(default="")
    price: float = Field(default=0.0)
    stop_loss: float = Field(default=0.0, description="Suggested stop-loss price")
    take_profit: float = Field(default=0.0, description="Suggested take-profit price")
    timestamp_ms: float = Field(default=0.0)
    metadata: dict[str, Any] = Field(default_factory=dict, description="Strategy-specific data")


class BaseStrategy(ABC):
    """Abstract base class for all trading strategies."""

    def __init__(self, config: StrategyConfig) -> None:
        self.config = config

    @abstractmethod
    async def on_data(self, market_data: dict[str, Any]) -> None:
        """Called when new market data arrives."""
        ...

    @abstractmethod
    async def generate_signal(self, metrics: Any) -> TradeSignal | None:
        """Evaluate conditions and return a TradeSignal or None.

        *metrics* is a MarketMetrics instance from the ImbalanceTracker.
        """
        ...

    @abstractmethod
    async def execute(self, order: Any) -> None:
        """Execute a validated order."""
        ...
