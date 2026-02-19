"""Telemetry Bridge — fire-and-forget event broadcasting via Redis Pub/Sub."""

from __future__ import annotations

import json
import os
import time
from typing import Any

from core.logger import logger

# redis is optional — bot runs fine without it
try:
    import redis.asyncio as aioredis

    _REDIS_AVAILABLE = True
except ImportError:
    aioredis = None  # type: ignore[assignment]
    _REDIS_AVAILABLE = False

CHANNEL = "rhizoo_telemetry"


class TelemetryClient:
    """Async Redis publisher for broadcasting bot state to external services.

    Fire-and-forget: if Redis is unreachable the bot continues trading.
    Supports both local Redis and hosted Redis with SSL/TLS + password auth.
    """

    def __init__(self) -> None:
        self._redis: Any | None = None
        self._enabled: bool = False

    async def connect(self) -> None:
        """Connect to Redis using environment variables.

        Env vars:
            REDIS_HOST     — hostname (default: localhost)
            REDIS_PORT     — port (default: 6379)
            REDIS_PASSWORD — auth password (default: None / no auth)
            REDIS_SSL      — set to "true" for TLS connections (default: false)
        """
        if not _REDIS_AVAILABLE:
            logger.info("[TELEMETRY] redis package not installed — telemetry disabled")
            return

        host = os.getenv("REDIS_HOST", "localhost")
        port = int(os.getenv("REDIS_PORT", "6379"))
        password = os.getenv("REDIS_PASSWORD") or None
        use_ssl = os.getenv("REDIS_SSL", "false").lower() == "true"

        try:
            self._redis = aioredis.Redis(
                host=host,
                port=port,
                password=password,
                ssl=use_ssl,
                decode_responses=True,
                socket_connect_timeout=5,
            )
            # Verify connectivity
            await self._redis.ping()
            self._enabled = True
            ssl_label = " (SSL/TLS)" if use_ssl else ""
            auth_label = " (authenticated)" if password else ""
            logger.info(
                f"[TELEMETRY] Connected to Redis at {host}:{port}"
                f"{ssl_label}{auth_label} — broadcasting on '{CHANNEL}'"
            )
        except Exception as exc:
            logger.warning(f"[TELEMETRY] Redis connection failed: {exc} — telemetry disabled")
            self._redis = None
            self._enabled = False

    async def broadcast_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """Publish a JSON event to the telemetry channel.

        Silently swallows errors — never interrupts the trading loop.
        """
        if not self._enabled:
            return

        message = {
            "event": event_type,
            "timestamp_ms": time.time() * 1000,
            "data": payload,
        }

        try:
            await self._redis.publish(CHANNEL, json.dumps(message, default=str))
        except Exception as exc:
            logger.debug(f"[TELEMETRY] Broadcast failed ({event_type}): {exc}")

    async def close(self) -> None:
        """Gracefully close the Redis connection."""
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:
                pass
            logger.info("[TELEMETRY] Redis connection closed")
