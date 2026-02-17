from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Any

import ccxt.pro as ccxtpro
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from core.logger import logger

load_dotenv()

MAX_RECONNECT_RETRIES = 5
BACKOFF_BASE_SECONDS = 2.0


class ExchangeConfig(BaseModel):
    exchange_id: str = Field(default="binance", description="CCXT exchange identifier")
    api_key: str = Field(default_factory=lambda: os.getenv("BINANCE_API_KEY", ""))
    secret: str = Field(default_factory=lambda: os.getenv("BINANCE_SECRET", ""))
    sandbox: bool = Field(default=True, description="Use testnet/sandbox mode")


class ExchangeClient:
    """Async exchange client powered by ccxt.pro with persistent WebSocket streams."""

    def __init__(self, config: ExchangeConfig | None = None) -> None:
        self.config = config or ExchangeConfig()
        self._exchange: ccxtpro.Exchange | None = None

    def _create_exchange(self) -> ccxtpro.Exchange:
        exchange_class = getattr(ccxtpro, self.config.exchange_id)
        exchange = exchange_class(
            {
                "apiKey": self.config.api_key,
                "secret": self.config.secret,
                "enableRateLimit": True,
            }
        )
        if self.config.sandbox:
            exchange.set_sandbox_mode(True)
        return exchange

    @property
    def exchange(self) -> ccxtpro.Exchange:
        if self._exchange is None:
            self._exchange = self._create_exchange()
        return self._exchange

    async def get_market_data(self, symbol: str = "BTC/USDT") -> dict[str, Any]:
        """Fetch current market data for a symbol via REST."""
        ticker = await self.exchange.fetch_ticker(symbol)
        return {
            "symbol": ticker["symbol"],
            "last": ticker["last"],
            "bid": ticker["bid"],
            "ask": ticker["ask"],
            "volume": ticker["baseVolume"],
        }

    async def get_bid_ask(self, symbol: str = "BTC/USDT") -> tuple[float, float]:
        """Fetch current best bid and ask prices via REST."""
        ticker = await self.exchange.fetch_ticker(symbol)
        return float(ticker["bid"]), float(ticker["ask"])

    async def stream_trades(self, symbol: str = "BTC/USDT") -> AsyncIterator[list[dict[str, Any]]]:
        """Persistent WebSocket trade stream with exponential backoff reconnection.

        Yields batches of trades as they arrive from the exchange.
        Reconnects automatically on connection drops (up to MAX_RECONNECT_RETRIES).
        """
        retries = 0

        while retries <= MAX_RECONNECT_RETRIES:
            try:
                logger.info(f"WebSocket connecting to {symbol} trade stream (attempt {retries + 1})")
                while True:
                    trades = await self.exchange.watch_trades(symbol)
                    if retries > 0:
                        logger.info(f"WebSocket reconnected to {symbol} after {retries} retries")
                        retries = 0
                    yield trades

            except (ccxtpro.NetworkError, ccxtpro.ExchangeNotAvailable) as exc:
                retries += 1
                if retries > MAX_RECONNECT_RETRIES:
                    logger.error(f"WebSocket max retries ({MAX_RECONNECT_RETRIES}) exceeded for {symbol}")
                    raise

                delay = BACKOFF_BASE_SECONDS ** retries
                logger.warning(
                    f"WebSocket dropped for {symbol}: {exc!r}. "
                    f"Reconnecting in {delay:.1f}s (retry {retries}/{MAX_RECONNECT_RETRIES})"
                )
                await asyncio.sleep(delay)

            except asyncio.CancelledError:
                logger.info(f"WebSocket stream for {symbol} cancelled")
                raise

    async def close(self) -> None:
        if self._exchange is not None:
            await self._exchange.close()
            logger.info("Exchange connection closed")
