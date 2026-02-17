from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from core.logger import logger


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class RiskConfig(BaseModel):
    account_balance: float = Field(
        default_factory=lambda: float(os.getenv("ACCOUNT_BALANCE", "10000.0")),
        description="Account equity in quote currency",
    )
    max_account_risk_pct: float = Field(
        default=0.01, description="Max risk per trade as fraction of balance (1%)"
    )
    max_daily_loss_pct: float = Field(
        default=0.03, description="Max cumulative daily loss before circuit breaker (3%)"
    )
    max_consecutive_losses: int = Field(
        default=3, description="Halt after N consecutive losing trades"
    )
    max_volatility_zscore: float = Field(
        default=4.0, description="Halt if volume Z-Score exceeds this"
    )
    max_spread_pct: float = Field(
        default=0.001, description="Slippage guard: reject if bid/ask spread > 0.1%"
    )
    reward_risk_ratio: float = Field(
        default=2.0, description="Take-profit distance as multiple of stop-loss distance"
    )
    min_order_qty: float = Field(
        default=0.001, description="Exchange minimum order quantity (base currency)"
    )


# ---------------------------------------------------------------------------
# Validated order — output of the Gatekeeper
# ---------------------------------------------------------------------------


class ValidatedOrder(BaseModel):
    """Fully validated order ready for execution."""

    side: str = Field(..., description="'buy' or 'sell'")
    entry_price: float
    stop_loss: float
    take_profit: float
    position_size: float = Field(..., description="Size in base currency")
    reason: str = Field(default="")
    timestamp_ms: float = Field(default=0.0)


# ---------------------------------------------------------------------------
# RiskManager — the Gatekeeper
# ---------------------------------------------------------------------------


class RiskManager:
    """Capital protection gate with daily tracking and circuit breakers."""

    def __init__(self, config: RiskConfig | None = None) -> None:
        self.config = config or RiskConfig()

        # Volatility circuit breaker
        self._volatility_halted: bool = False

        # Daily tracker — resets at 00:00 UTC
        self._current_day: str = self._today_utc()
        self._daily_pnl: float = 0.0
        self._consecutive_losses: int = 0
        self._daily_halted: bool = False

        logger.info(
            f"RiskManager initialized "
            f"(balance={self.config.account_balance}, "
            f"risk/trade={self.config.max_account_risk_pct:.1%}, "
            f"daily_limit={self.config.max_daily_loss_pct:.1%}, "
            f"max_consec_losses={self.config.max_consecutive_losses})"
        )

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _today_utc() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _check_day_rollover(self) -> None:
        """Reset daily counters at 00:00 UTC."""
        today = self._today_utc()
        if today != self._current_day:
            logger.info(
                f"New trading day {today} — resetting daily tracker "
                f"(prev PnL: {self._daily_pnl:+.2f})"
            )
            self._current_day = today
            self._daily_pnl = 0.0
            self._consecutive_losses = 0
            self._daily_halted = False

    # -- metrics ingestion -------------------------------------------------

    def update_metrics(self, metrics: Any) -> None:
        """Ingest MarketMetrics for the volatility circuit breaker."""
        self._check_day_rollover()

        if metrics.volume_zscore >= self.config.max_volatility_zscore:
            if not self._volatility_halted:
                logger.warning(
                    f"VOLATILITY BREAKER: Z-Score {metrics.volume_zscore} "
                    f">= {self.config.max_volatility_zscore} — halting signals"
                )
            self._volatility_halted = True
        else:
            if self._volatility_halted:
                logger.info("Volatility breaker released — resuming")
            self._volatility_halted = False

    # -- position sizing ---------------------------------------------------

    def calculate_position_size(
        self, entry_price: float, stop_loss_price: float
    ) -> float:
        """Dynamic position sizing based on account risk.

        Size = (Balance * Risk_Per_Trade) / |Entry - StopLoss|
        Clamped to min_order_qty and max_position (balance / entry).
        """
        risk_distance: float = abs(entry_price - stop_loss_price)
        if risk_distance == 0.0:
            return 0.0

        risk_amount: float = self.config.account_balance * self.config.max_account_risk_pct
        size: float = risk_amount / risk_distance

        # Clamp to exchange minimum
        if size < self.config.min_order_qty:
            return 0.0

        # Clamp to max affordable (no leverage)
        max_size: float = self.config.account_balance / entry_price
        size = min(size, max_size)

        return round(size, 8)

    # -- the Gatekeeper ----------------------------------------------------

    def process_signal(
        self,
        signal: Any,
        bid: float,
        ask: float,
    ) -> ValidatedOrder | None:
        """Validate a TradeSignal and return a ValidatedOrder or None.

        Checks (in order):
        1. Daily loss circuit breaker
        2. Consecutive loss limit
        3. Volatility circuit breaker
        4. Slippage guard (spread check)
        5. Stop-loss validity
        6. Position sizing
        """
        self._check_day_rollover()

        # 1. Daily loss circuit breaker
        if self._daily_halted:
            logger.debug("Signal rejected: daily circuit breaker active")
            return None

        daily_loss_limit: float = self.config.account_balance * self.config.max_daily_loss_pct
        if self._daily_pnl <= -daily_loss_limit:
            self._daily_halted = True
            logger.critical(
                f"CIRCUIT BREAKER TRIGGERED. Daily loss {self._daily_pnl:+.2f} "
                f"hit limit -{daily_loss_limit:.2f}. SHUTTING DOWN."
            )
            return None

        # 2. Consecutive loss limit
        if self._consecutive_losses >= self.config.max_consecutive_losses:
            logger.warning(
                f"Signal rejected: {self._consecutive_losses} consecutive losses "
                f"(limit={self.config.max_consecutive_losses})"
            )
            return None

        # 3. Volatility circuit breaker
        if self._volatility_halted:
            logger.debug("Signal rejected: volatility circuit breaker active")
            return None

        # 4. Slippage guard
        if bid <= 0 or ask <= 0:
            logger.warning("Signal rejected: invalid bid/ask")
            return None

        mid_price: float = (bid + ask) / 2.0
        spread_pct: float = (ask - bid) / mid_price
        if spread_pct > self.config.max_spread_pct:
            logger.warning(
                f"Signal rejected: spread {spread_pct:.4%} > "
                f"limit {self.config.max_spread_pct:.4%}"
            )
            return None

        # 5. Determine entry, SL, TP
        entry_price: float = ask if signal.side == "buy" else bid
        stop_loss: float = signal.stop_loss if signal.stop_loss > 0 else 0.0

        if stop_loss == 0.0:
            logger.warning("Signal rejected: no stop-loss provided")
            return None

        # Sanity: SL must be on the correct side of entry
        if signal.side == "buy" and stop_loss >= entry_price:
            logger.warning("Signal rejected: stop-loss >= entry for a buy")
            return None
        if signal.side == "sell" and stop_loss <= entry_price:
            logger.warning("Signal rejected: stop-loss <= entry for a sell")
            return None

        risk_distance: float = abs(entry_price - stop_loss)
        if signal.side == "buy":
            take_profit: float = entry_price + risk_distance * self.config.reward_risk_ratio
        else:
            take_profit = entry_price - risk_distance * self.config.reward_risk_ratio

        # 6. Position sizing
        size: float = self.calculate_position_size(entry_price, stop_loss)
        if size == 0.0:
            logger.warning("Signal rejected: calculated position size is zero / below minimum")
            return None

        order = ValidatedOrder(
            side=signal.side,
            entry_price=round(entry_price, 8),
            stop_loss=round(stop_loss, 8),
            take_profit=round(take_profit, 8),
            position_size=size,
            reason=signal.reason,
            timestamp_ms=signal.timestamp_ms,
        )

        logger.info(
            f"Order validated: {order.side.upper()} {order.position_size:.6f} "
            f"@ {order.entry_price:.2f} | SL={order.stop_loss:.2f} "
            f"TP={order.take_profit:.2f}"
        )
        return order

    # -- trade result recording --------------------------------------------

    def record_fill(self, pnl: float) -> None:
        """Record a closed trade PnL for daily tracking."""
        self._check_day_rollover()
        self._daily_pnl += pnl

        if pnl < 0:
            self._consecutive_losses += 1
            logger.info(
                f"Loss recorded: {pnl:+.2f} "
                f"(consecutive={self._consecutive_losses}, daily={self._daily_pnl:+.2f})"
            )
        else:
            self._consecutive_losses = 0
            logger.info(f"Win recorded: {pnl:+.2f} (daily={self._daily_pnl:+.2f})")

        # Check if daily limit is now breached
        daily_loss_limit: float = self.config.account_balance * self.config.max_daily_loss_pct
        if self._daily_pnl <= -daily_loss_limit and not self._daily_halted:
            self._daily_halted = True
            logger.critical(
                f"CIRCUIT BREAKER TRIGGERED. Daily loss {self._daily_pnl:+.2f} "
                f"hit limit -{daily_loss_limit:.2f}. SHUTTING DOWN."
            )
