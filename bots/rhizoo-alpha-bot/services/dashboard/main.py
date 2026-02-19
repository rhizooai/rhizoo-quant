"""Rhizoo Watch Window — real-time charting dashboard powered by FastAPI + Redis Pub/Sub."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

load_dotenv()

logger = logging.getLogger("rhizoo.dashboard")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s - %(message)s",
)

CHANNEL = "rhizoo_telemetry"
HEARTBEAT_INTERVAL = 15  # seconds

# ---------------------------------------------------------------------------
# Connection manager — per-symbol WebSocket fan-out
# ---------------------------------------------------------------------------


class ConnectionManager:
    """Manages WebSocket clients grouped by symbol."""

    def __init__(self) -> None:
        # symbol → set of WebSocket connections
        self._clients: dict[str, set[WebSocket]] = {}

    async def connect(self, ws: WebSocket, symbol: str) -> None:
        await ws.accept()
        self._clients.setdefault(symbol, set()).add(ws)
        logger.info(f"Client connected: {symbol} (total: {len(self._clients[symbol])})")

    def disconnect(self, ws: WebSocket, symbol: str) -> None:
        if symbol in self._clients:
            self._clients[symbol].discard(ws)
            if not self._clients[symbol]:
                del self._clients[symbol]
        logger.info(f"Client disconnected: {symbol}")

    async def broadcast(self, symbol: str, message: str) -> None:
        """Send message to all clients subscribed to a symbol."""
        clients = self._clients.get(symbol, set())
        dead: list[WebSocket] = []
        for ws in clients:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            clients.discard(ws)

    async def broadcast_all(self, message: str) -> None:
        """Send message to ALL connected clients regardless of symbol."""
        for symbol in list(self._clients.keys()):
            await self.broadcast(symbol, message)

    @property
    def all_clients(self) -> set[WebSocket]:
        result: set[WebSocket] = set()
        for clients in self._clients.values():
            result |= clients
        return result


manager = ConnectionManager()

# ---------------------------------------------------------------------------
# Redis subscriber background task
# ---------------------------------------------------------------------------


async def _redis_subscriber() -> None:
    """Subscribe to the telemetry channel and fan-out events to WebSocket clients."""
    host = os.getenv("REDIS_HOST", "localhost")
    port = int(os.getenv("REDIS_PORT", "6379"))
    password = os.getenv("REDIS_PASSWORD") or None
    use_ssl = os.getenv("REDIS_SSL", "false").lower() == "true"

    while True:
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
            logger.info(f"Redis connected at {host}:{port} — subscribing to '{CHANNEL}'")

            pubsub = r.pubsub()
            await pubsub.subscribe(CHANNEL)

            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue

                raw = message["data"]

                # Route to symbol-specific clients
                try:
                    parsed = json.loads(raw)
                    symbol = parsed.get("data", {}).get("symbol") or parsed.get("data", {}).get("pair")
                    if symbol:
                        await manager.broadcast(symbol, raw)
                    else:
                        # No symbol in payload — broadcast to all
                        await manager.broadcast_all(raw)
                except json.JSONDecodeError:
                    await manager.broadcast_all(raw)

        except asyncio.CancelledError:
            logger.info("Redis subscriber cancelled")
            break
        except Exception as exc:
            logger.warning(f"Redis subscriber error: {exc} — reconnecting in 5s")
            await asyncio.sleep(5)
        finally:
            try:
                await pubsub.unsubscribe(CHANNEL)
                await pubsub.aclose()
                await r.aclose()
            except Exception:
                pass


async def _heartbeat() -> None:
    """Send periodic pings to all WebSocket clients to keep connections alive."""
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        ping_msg = json.dumps({"event": "HEARTBEAT"})
        for ws in list(manager.all_clients):
            try:
                await ws.send_text(ping_msg)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

_bg_tasks: list[asyncio.Task[Any]] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    _bg_tasks.append(asyncio.create_task(_redis_subscriber()))
    _bg_tasks.append(asyncio.create_task(_heartbeat()))
    logger.info("Dashboard started — background tasks running")
    yield
    for t in _bg_tasks:
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
    logger.info("Dashboard shutdown complete")


app = FastAPI(title="Rhizoo Watch Window", lifespan=lifespan)


@app.websocket("/ws/{symbol}")
async def websocket_endpoint(ws: WebSocket, symbol: str) -> None:
    symbol = symbol.upper().replace("_", "/")
    await manager.connect(ws, symbol)
    try:
        while True:
            # Keep the connection alive by reading (client can send pongs or other msgs)
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws, symbol)
    except Exception:
        manager.disconnect(ws, symbol)


# Serve static frontend files
_static_dir = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="static")
