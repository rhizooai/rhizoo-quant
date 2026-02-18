"""Paper trading broker — virtual execution with realistic slippage/commission."""

from __future__ import annotations

import csv
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.logger import logger


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class PaperPosition:
    id: str
    timestamp_ms: float
    pair: str
    side: str
    entry_price: float
    stop_loss: float
    take_profit: float
    position_size: float
    reason: str


@dataclass
class ClosedTrade:
    id: str
    timestamp_ms: float
    pair: str
    side: str
    entry_price: float
    stop_loss: float
    take_profit: float
    position_size: float
    reason: str
    exit_price: float
    pnl: float
    result: str  # "WIN" or "LOSS"
    timestamp_close: float


# ---------------------------------------------------------------------------
# PaperBroker
# ---------------------------------------------------------------------------

_COMMISSION_PCT = 0.0005  # 0.05 % per side

_CSV_COLUMNS = [
    "id", "timestamp", "pair", "side", "entry", "sl", "tp",
    "size", "exit_price", "pnl", "result",
]


class PaperBroker:
    """Virtual broker that simulates order execution with slippage and commission."""

    def __init__(
        self,
        pair: str = "BTC/USDT",
        virtual_balance: float | None = None,
        csv_path: str | None = None,
    ) -> None:
        self.pair = pair
        self.virtual_balance = virtual_balance or float(
            os.getenv("PAPER_BALANCE", "10000.0")
        )
        self._initial_balance = self.virtual_balance

        default_csv = f"logs/simulated_trades_{pair.replace('/', '_')}.csv"
        self.csv_path = Path(csv_path or default_csv)
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)

        self.active_positions: list[PaperPosition] = []

        # Stats accumulators
        self._closed_trades: list[ClosedTrade] = []
        self._gross_win: float = 0.0
        self._gross_loss: float = 0.0
        self._peak_balance: float = self.virtual_balance
        self._max_drawdown_pct: float = 0.0

        # Write CSV header if file doesn't exist
        if not self.csv_path.exists():
            with open(self.csv_path, "w", newline="") as f:
                csv.writer(f).writerow(_CSV_COLUMNS)

        logger.info(
            f"PaperBroker initialized — pair={pair}, "
            f"balance={self.virtual_balance:.2f}, csv={self.csv_path}"
        )

    # -- execution ---------------------------------------------------------

    def execute_order(self, order: Any) -> PaperPosition:
        """Simulate order fill with 0.05% slippage + commission applied to entry."""
        entry = order.entry_price
        if order.side == "buy":
            entry *= 1 + _COMMISSION_PCT  # shift up for buys
        else:
            entry *= 1 - _COMMISSION_PCT  # shift down for sells

        entry = round(entry, 8)

        pos = PaperPosition(
            id=uuid.uuid4().hex[:8],
            timestamp_ms=order.timestamp_ms or time.time() * 1000,
            pair=self.pair,
            side=order.side,
            entry_price=entry,
            stop_loss=order.stop_loss,
            take_profit=order.take_profit,
            position_size=order.position_size,
            reason=order.reason,
        )
        self.active_positions.append(pos)

        logger.info(
            f"[PAPER] Opened {pos.side.upper()} {pos.position_size:.6f} "
            f"@ {pos.entry_price:.2f} (id={pos.id})"
        )
        return pos

    # -- closing -----------------------------------------------------------

    def close_position(
        self, position: PaperPosition, exit_price: float, result: str
    ) -> ClosedTrade:
        """Close a position, apply exit commission, compute PnL, update stats."""
        # Apply 0.05% exit commission
        if position.side == "buy":
            adjusted_exit = exit_price * (1 - _COMMISSION_PCT)
        else:
            adjusted_exit = exit_price * (1 + _COMMISSION_PCT)
        adjusted_exit = round(adjusted_exit, 8)

        # PnL
        if position.side == "buy":
            pnl = (adjusted_exit - position.entry_price) * position.position_size
        else:
            pnl = (position.entry_price - adjusted_exit) * position.position_size
        pnl = round(pnl, 8)

        ct = ClosedTrade(
            id=position.id,
            timestamp_ms=position.timestamp_ms,
            pair=position.pair,
            side=position.side,
            entry_price=position.entry_price,
            stop_loss=position.stop_loss,
            take_profit=position.take_profit,
            position_size=position.position_size,
            reason=position.reason,
            exit_price=adjusted_exit,
            pnl=pnl,
            result=result,
            timestamp_close=time.time() * 1000,
        )

        # Remove from active
        self.active_positions = [
            p for p in self.active_positions if p.id != position.id
        ]

        # Update balance and stats
        self.virtual_balance += pnl
        self._closed_trades.append(ct)

        if pnl >= 0:
            self._gross_win += pnl
        else:
            self._gross_loss += abs(pnl)

        # Drawdown tracking
        if self.virtual_balance > self._peak_balance:
            self._peak_balance = self.virtual_balance
        dd_pct = (
            (self._peak_balance - self.virtual_balance) / self._peak_balance * 100
            if self._peak_balance > 0
            else 0.0
        )
        if dd_pct > self._max_drawdown_pct:
            self._max_drawdown_pct = dd_pct

        # Append to CSV
        self._write_csv_row(ct)

        logger.info(
            f"[PAPER] Closed {ct.side.upper()} id={ct.id} — "
            f"{ct.result} PnL={ct.pnl:+.2f} | balance={self.virtual_balance:.2f}"
        )
        return ct

    # -- stats -------------------------------------------------------------

    def get_stats(self) -> dict[str, Any]:
        total = len(self._closed_trades)
        wins = sum(1 for t in self._closed_trades if t.result == "WIN")
        win_rate = (wins / total * 100) if total > 0 else 0.0
        profit_factor = (
            self._gross_win / self._gross_loss
            if self._gross_loss > 0
            else float("inf") if self._gross_win > 0 else 0.0
        )
        net_pnl = self.virtual_balance - self._initial_balance

        return {
            "win_rate_pct": round(win_rate, 1),
            "profit_factor": round(profit_factor, 2),
            "max_drawdown_pct": round(self._max_drawdown_pct, 2),
            "total_trades": total,
            "net_pnl": round(net_pnl, 2),
            "virtual_balance": round(self.virtual_balance, 2),
            "active_positions": len(self.active_positions),
        }

    # -- internals ---------------------------------------------------------

    def _write_csv_row(self, ct: ClosedTrade) -> None:
        with open(self.csv_path, "a", newline="") as f:
            csv.writer(f).writerow([
                ct.id,
                ct.timestamp_ms,
                ct.pair,
                ct.side,
                ct.entry_price,
                ct.stop_loss,
                ct.take_profit,
                ct.position_size,
                ct.exit_price,
                ct.pnl,
                ct.result,
            ])
