from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field


class StrategyConfig(BaseModel):
    name: str = Field(..., description="Strategy identifier")
    symbol: str = Field(default="BTC/USDT", description="Trading pair")
    timeframe: str = Field(default="5m", description="Candle timeframe")


class BaseStrategy(ABC):
    """Abstract base class for all trading strategies."""

    def __init__(self, config: StrategyConfig) -> None:
        self.config = config

    @abstractmethod
    async def on_data(self, market_data: dict[str, Any]) -> None:
        """Called when new market data arrives."""
        ...

    @abstractmethod
    async def generate_signal(self) -> str | None:
        """Evaluate conditions and return a signal ('buy', 'sell') or None."""
        ...

    @abstractmethod
    async def execute(self, signal: str) -> None:
        """Execute a trade based on the signal."""
        ...
