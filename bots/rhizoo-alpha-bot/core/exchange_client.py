from __future__ import annotations

import os
from typing import Any

import ccxt.pro as ccxtpro
from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()


class ExchangeConfig(BaseModel):
    exchange_id: str = Field(default="binance", description="CCXT exchange identifier")
    api_key: str = Field(default_factory=lambda: os.getenv("BINANCE_API_KEY", ""))
    secret: str = Field(default_factory=lambda: os.getenv("BINANCE_SECRET", ""))
    sandbox: bool = Field(default=True, description="Use testnet/sandbox mode")


class ExchangeClient:
    """Async exchange client powered by ccxt.pro."""

    def __init__(self, config: ExchangeConfig | None = None) -> None:
        self.config = config or ExchangeConfig()
        exchange_class = getattr(ccxtpro, self.config.exchange_id)
        self.exchange: ccxtpro.Exchange = exchange_class(
            {
                "apiKey": self.config.api_key,
                "secret": self.config.secret,
                "enableRateLimit": True,
            }
        )
        if self.config.sandbox:
            self.exchange.set_sandbox_mode(True)

    async def get_market_data(self, symbol: str = "BTC/USDT") -> dict[str, Any]:
        """Fetch current market data for a symbol.

        Placeholder — will be expanded with OHLCV, order book, and trade feeds.
        """
        ticker = await self.exchange.fetch_ticker(symbol)
        return {
            "symbol": ticker["symbol"],
            "last": ticker["last"],
            "bid": ticker["bid"],
            "ask": ticker["ask"],
            "volume": ticker["baseVolume"],
        }

    async def close(self) -> None:
        await self.exchange.close()
