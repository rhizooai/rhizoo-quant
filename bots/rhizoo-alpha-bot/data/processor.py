from __future__ import annotations

from collections import deque
from typing import Any

from pydantic import BaseModel, Field

from core.logger import logger

DEFAULT_BUFFER_SIZE = 1000


class BufferConfig(BaseModel):
    max_size: int = Field(default=DEFAULT_BUFFER_SIZE, description="Maximum trades held in buffer")


class DataBuffer:
    """Fixed-size trade buffer backed by collections.deque.

    Maintains the last N trades/ticks for downstream strategy consumption.
    """

    def __init__(self, config: BufferConfig | None = None) -> None:
        self.config = config or BufferConfig()
        self._buffer: deque[dict[str, Any]] = deque(maxlen=self.config.max_size)
        logger.info(f"DataBuffer initialized (max_size={self.config.max_size})")

    def push(self, trades: list[dict[str, Any]]) -> None:
        """Append a batch of trades to the buffer."""
        for trade in trades:
            self._buffer.append(trade)

    def snapshot(self) -> list[dict[str, Any]]:
        """Return a copy of all buffered trades."""
        return list(self._buffer)

    @property
    def size(self) -> int:
        return len(self._buffer)

    @property
    def full(self) -> bool:
        return self.size == self.config.max_size

    def clear(self) -> None:
        self._buffer.clear()
