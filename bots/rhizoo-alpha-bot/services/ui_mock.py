#!/usr/bin/env python3
"""Mock Telemetry Listener — subscribes to the Redis channel and prints events.

Usage:
    source .venv/bin/activate
    python services/ui_mock.py

Reads REDIS_HOST, REDIS_PORT, REDIS_PASSWORD, REDIS_SSL from .env (same as the bot).
Press Ctrl+C to stop.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()  # pick up .env in cwd or parent

CHANNEL = "rhizoo_telemetry"

# ANSI colors for terminal output
_COLORS = {
    "MARKET_PULSE": "\033[36m",   # cyan
    "LEVEL_UPDATE": "\033[33m",   # yellow
    "SIGNAL_GEN":   "\033[35m",   # magenta
    "TRADE_UPDATE": "\033[32m",   # green
}
_RESET = "\033[0m"
_BOLD = "\033[1m"


def _format_event(raw: str) -> str:
    """Pretty-print a telemetry JSON event."""
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return raw

    event_type = msg.get("event", "UNKNOWN")
    ts_ms = msg.get("timestamp_ms", 0)
    data = msg.get("data", {})

    ts_str = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%H:%M:%S.%f")[:-3]
    color = _COLORS.get(event_type, "")

    data_str = json.dumps(data, indent=2, default=str)
    return f"{color}{_BOLD}[{ts_str}] {event_type}{_RESET}\n{data_str}"


async def listen() -> None:
    try:
        import redis.asyncio as aioredis
    except ImportError:
        print(
            "[ERROR] redis package not installed.\n"
            "Run: pip install 'redis[hiredis]>=5.0.0'"
        )
        sys.exit(1)

    host = os.getenv("REDIS_HOST", "localhost")
    port = int(os.getenv("REDIS_PORT", "6379"))
    password = os.getenv("REDIS_PASSWORD") or None
    use_ssl = os.getenv("REDIS_SSL", "false").lower() == "true"

    print(f"Connecting to Redis at {host}:{port} ...")

    try:
        r = aioredis.Redis(
            host=host,
            port=port,
            password=password,
            ssl=use_ssl,
            decode_responses=True,
            socket_connect_timeout=5,
        )
        await r.ping()
    except Exception as exc:
        print(f"[ERROR] Could not connect to Redis: {exc}")
        sys.exit(1)

    print(f"Subscribed to '{CHANNEL}'. Waiting for events ...\n")

    pubsub = r.pubsub()
    await pubsub.subscribe(CHANNEL)

    try:
        async for message in pubsub.listen():
            if message["type"] == "message":
                print(_format_event(message["data"]))
                print()
    except asyncio.CancelledError:
        pass
    finally:
        await pubsub.unsubscribe(CHANNEL)
        await pubsub.aclose()
        await r.aclose()
        print("Listener stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(listen())
    except KeyboardInterrupt:
        print("\nShutting down.")
