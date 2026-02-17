from __future__ import annotations

import os
import time
from collections import deque
from typing import Any

import numpy as np
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from core.logger import logger

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class ImbalanceConfig(BaseModel):
    max_buffer_size: int = Field(default=50_000, description="Hard cap on raw trade storage")
    nofi_window_sec: float = Field(default=60.0, description="Sliding window for nOFI (seconds)")
    volume_window_min: float = Field(default=20.0, description="Look-back for volume Z-Score (minutes)")
    zscore_threshold: float = Field(
        default_factory=lambda: float(os.getenv("ZSCORE_THRESHOLD", "2.0")),
        description="Z-Score threshold to flag significant volume",
    )
    absorption_nofi_min: float = Field(
        default=0.4, description="Minimum |nOFI| to consider for absorption"
    )
    absorption_eff_max: float = Field(
        default=0.0001, description="Maximum |efficiency| to classify as absorption"
    )


class LevelConfig(BaseModel):
    candle_interval_sec: float = Field(default=60.0, description="Candle synthesis interval (seconds)")
    candle_window: int = Field(default=200, description="Rolling candle count for level detection")
    atr_period: int = Field(default=14, description="ATR lookback period")
    atr_buffer_mult: float = Field(default=0.1, description="ATR multiplier for liquidity zone")
    snap_back_sec: float = Field(default=30.0, description="Max time for price to snap back after sweep")
    nofi_threshold: float = Field(default=0.75, description="Minimum |nOFI| to confirm a sweep")
    cooldown_sec: float = Field(default=300.0, description="Cooldown after a sweep signal (seconds)")


# ---------------------------------------------------------------------------
# Metrics / Info models
# ---------------------------------------------------------------------------


class MarketMetrics(BaseModel):
    nofi: float = Field(default=0.0, description="Normalized Order Flow Imbalance [-1, 1]")
    buy_volume: float = Field(default=0.0)
    sell_volume: float = Field(default=0.0)
    efficiency: float = Field(default=0.0, description="Price efficiency (dp / total_vol)")
    volume_zscore: float = Field(default=0.0, description="Current 1-min volume Z-Score")
    is_significant: bool = Field(default=False, description="Volume Z-Score > threshold")
    is_absorption: bool = Field(default=False, description="High |nOFI| + near-zero efficiency")
    trend: str = Field(default="NEUTRAL")
    status: str = Field(default="MONITORING")


class LevelInfo(BaseModel):
    nearest_high: float = Field(default=0.0)
    nearest_low: float = Field(default=0.0)
    high_distance_pct: float = Field(default=0.0)
    low_distance_pct: float = Field(default=0.0)
    atr: float = Field(default=0.0)
    sweep_status: str = Field(default="SCANNING")
    current_price: float = Field(default=0.0)


# ---------------------------------------------------------------------------
# ImbalanceTracker (unchanged)
# ---------------------------------------------------------------------------


class ImbalanceTracker:
    """Sliding-window imbalance & volume-weighting engine.

    All math is vectorised with NumPy to keep per-tick latency < 10 ms.
    """

    def __init__(self, config: ImbalanceConfig | None = None) -> None:
        self.config = config or ImbalanceConfig()

        # Raw trade store: each entry is (timestamp_ms, side_is_buy, price, amount)
        self._trades: deque[tuple[float, bool, float, float]] = deque(
            maxlen=self.config.max_buffer_size
        )

        logger.info(
            f"ImbalanceTracker initialized "
            f"(nofi_window={self.config.nofi_window_sec}s, "
            f"vol_window={self.config.volume_window_min}min, "
            f"zscore_thresh={self.config.zscore_threshold})"
        )

    # -- ingestion ---------------------------------------------------------

    def push(self, trades: list[dict[str, Any]]) -> None:
        """Ingest a batch of ccxt trade dicts."""
        for t in trades:
            self._trades.append((
                float(t["timestamp"]),
                t["side"] == "buy",
                float(t["price"]),
                float(t["amount"]),
            ))

    @property
    def size(self) -> int:
        return len(self._trades)

    # -- numpy helpers -----------------------------------------------------

    def _window(self, seconds: float) -> np.ndarray:
        """Return trades within the last *seconds* as a structured ndarray.

        Columns: [timestamp_ms, is_buy (0/1), price, amount]
        """
        if not self._trades:
            return np.empty((0, 4), dtype=np.float64)

        cutoff = time.time() * 1000 - seconds * 1000
        rows: list[tuple[float, bool, float, float]] = []
        for entry in reversed(self._trades):
            if entry[0] < cutoff:
                break
            rows.append(entry)

        if not rows:
            return np.empty((0, 4), dtype=np.float64)

        arr = np.array(rows, dtype=np.float64)
        return arr[::-1]

    # -- core metrics ------------------------------------------------------

    def compute_nofi(self, window: np.ndarray) -> tuple[float, float, float]:
        """Normalised Order Flow Imbalance. Returns (nOFI, buy_volume, sell_volume)."""
        if len(window) == 0:
            return 0.0, 0.0, 0.0

        is_buy = window[:, 1].astype(bool)
        amounts = window[:, 3]

        v_buy = float(np.sum(amounts[is_buy]))
        v_sell = float(np.sum(amounts[~is_buy]))
        total = v_buy + v_sell

        if total == 0:
            return 0.0, v_buy, v_sell

        nofi = (v_buy - v_sell) / total
        return nofi, v_buy, v_sell

    def compute_efficiency(self, window: np.ndarray) -> float:
        """Price efficiency = (price_end - price_start) / total_volume."""
        if len(window) < 2:
            return 0.0

        price_start = float(window[0, 2])
        price_end = float(window[-1, 2])
        total_vol = float(np.sum(window[:, 3]))

        if total_vol == 0:
            return 0.0

        return (price_end - price_start) / total_vol

    def compute_volume_zscore(self) -> float:
        """Z-Score of current 1-min volume vs trailing 20-min distribution."""
        window_sec = self.config.volume_window_min * 60
        arr = self._window(window_sec)
        if len(arr) == 0:
            return 0.0

        bucket_ms = 60_000.0
        timestamps = arr[:, 0]
        amounts = arr[:, 3]

        bucket_ids = ((timestamps - timestamps[0]) // bucket_ms).astype(np.int64)

        max_bucket = int(bucket_ids[-1]) + 1
        bucket_vols = np.zeros(max_bucket, dtype=np.float64)
        np.add.at(bucket_vols, bucket_ids, amounts)

        if len(bucket_vols) < 2:
            return 0.0

        current_vol = bucket_vols[-1]
        hist_vols = bucket_vols[:-1]

        mean = float(np.mean(hist_vols))
        std = float(np.std(hist_vols, ddof=1)) if len(hist_vols) > 1 else 0.0

        if std == 0:
            return 0.0

        return (current_vol - mean) / std

    # -- aggregate ---------------------------------------------------------

    def compute_metrics(self) -> MarketMetrics:
        """Compute all metrics in one pass — called per tick batch."""
        window = self._window(self.config.nofi_window_sec)
        nofi, v_buy, v_sell = self.compute_nofi(window)
        efficiency = self.compute_efficiency(window)
        zscore = self.compute_volume_zscore()

        is_significant = zscore > self.config.zscore_threshold
        is_absorption = (
            abs(nofi) >= self.config.absorption_nofi_min
            and abs(efficiency) <= self.config.absorption_eff_max
        )

        if nofi > 0.3:
            trend = "BULLISH"
        elif nofi < -0.3:
            trend = "BEARISH"
        else:
            trend = "NEUTRAL"

        status = "SIGNAL_DETECTED" if is_significant else "MONITORING"

        return MarketMetrics(
            nofi=round(nofi, 4),
            buy_volume=round(v_buy, 6),
            sell_volume=round(v_sell, 6),
            efficiency=round(efficiency, 6),
            volume_zscore=round(zscore, 2),
            is_significant=is_significant,
            is_absorption=is_absorption,
            trend=trend,
            status=status,
        )


# ---------------------------------------------------------------------------
# LevelTracker — candle synthesis, local high/low, ATR, sweep detection
# ---------------------------------------------------------------------------

# Candle columns: [open, high, low, close, volume, timestamp_ms_open]
_O, _H, _L, _C, _V, _T = 0, 1, 2, 3, 4, 5


class LevelTracker:
    """Tracks liquidity levels from synthesised candles and detects sweep events."""

    # Sweep state machine
    _STATE_SCANNING = "SCANNING"
    _STATE_SWEEP_LOW = "SWEEP_LOW"
    _STATE_SWEEP_HIGH = "SWEEP_HIGH"
    _STATE_COOLDOWN = "COOLDOWN"

    def __init__(self, config: LevelConfig | None = None) -> None:
        self.config = config or LevelConfig()

        self._candles: deque[np.ndarray] = deque(maxlen=self.config.candle_window)
        self._current_candle: np.ndarray | None = None
        self._candle_open_ts: float = 0.0

        self._current_price: float = 0.0
        self._local_high: float = 0.0
        self._local_low: float = 0.0
        self._atr: float = 0.0

        # Sweep state
        self._state: str = self._STATE_SCANNING
        self._sweep_start_time: float = 0.0
        self._cooldown_start: float = 0.0

        logger.info(
            f"LevelTracker initialized "
            f"(window={self.config.candle_window} candles, "
            f"atr_period={self.config.atr_period}, "
            f"cooldown={self.config.cooldown_sec}s)"
        )

    # -- candle synthesis --------------------------------------------------

    def _bucket_ts(self, ts_ms: float) -> float:
        """Floor timestamp to the candle interval boundary."""
        interval_ms = self.config.candle_interval_sec * 1000
        return (ts_ms // interval_ms) * interval_ms

    def push_trade(self, trade: dict[str, Any]) -> None:
        """Feed a single trade — builds candles incrementally."""
        ts = float(trade["timestamp"])
        price = float(trade["price"])
        amount = float(trade["amount"])
        self._current_price = price

        bucket = self._bucket_ts(ts)

        if self._current_candle is None or bucket != self._candle_open_ts:
            # Finalise previous candle
            if self._current_candle is not None:
                self._candles.append(self._current_candle.copy())
                self._recompute_levels()
            # Start new candle
            self._current_candle = np.array(
                [price, price, price, price, amount, bucket], dtype=np.float64
            )
            self._candle_open_ts = bucket
        else:
            c = self._current_candle
            c[_H] = max(c[_H], price)
            c[_L] = min(c[_L], price)
            c[_C] = price
            c[_V] += amount

    # -- level computation -------------------------------------------------

    def _recompute_levels(self) -> None:
        """Recompute local high/low and ATR from finalised candles."""
        if len(self._candles) < 2:
            return

        candles = np.array(self._candles, dtype=np.float64)
        highs = candles[:, _H]
        lows = candles[:, _L]
        closes = candles[:, _C]

        self._local_high = float(np.max(highs))
        self._local_low = float(np.min(lows))

        # ATR
        n = min(self.config.atr_period, len(candles))
        recent = candles[-n:]
        if n >= 2:
            h = recent[:, _H]
            l = recent[:, _L]
            prev_c = np.empty_like(h)
            prev_c[0] = recent[0, _O]
            prev_c[1:] = recent[:-1, _C]

            tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
            self._atr = float(np.mean(tr))

    # -- sweep detection ---------------------------------------------------

    def check_sweep(self, nofi: float) -> tuple[str, str] | None:
        """Evaluate the sweep state machine.

        Returns (side, strength) if a valid sweep signal fires, else None.
        Sides: "buy" (bearish sweep confirmed) or "sell" (bullish sweep confirmed).
        """
        if self._local_high == 0.0 or self._local_low == 0.0 or self._current_price == 0.0:
            return None

        now = time.time()
        buf = self._atr * self.config.atr_buffer_mult

        # --- COOLDOWN ---
        if self._state == self._STATE_COOLDOWN:
            if now - self._cooldown_start >= self.config.cooldown_sec:
                self._state = self._STATE_SCANNING
                logger.info("Sweep cooldown expired — back to SCANNING")
            return None

        # --- SWEEP_LOW: waiting for snap-back + buy imbalance ---
        if self._state == self._STATE_SWEEP_LOW:
            elapsed = now - self._sweep_start_time
            if elapsed > self.config.snap_back_sec:
                logger.debug("Sweep LOW timed out — back to SCANNING")
                self._state = self._STATE_SCANNING
            elif self._current_price > self._local_low and nofi >= self.config.nofi_threshold:
                self._state = self._STATE_COOLDOWN
                self._cooldown_start = now
                logger.info(
                    f"BEARISH SWEEP CONFIRMED at {self._current_price:.2f} "
                    f"(nOFI={nofi:+.2f}) → BUY"
                )
                return ("buy", "HIGH")
            return None

        # --- SWEEP_HIGH: waiting for snap-back + sell imbalance ---
        if self._state == self._STATE_SWEEP_HIGH:
            elapsed = now - self._sweep_start_time
            if elapsed > self.config.snap_back_sec:
                logger.debug("Sweep HIGH timed out — back to SCANNING")
                self._state = self._STATE_SCANNING
            elif self._current_price < self._local_high and nofi <= -self.config.nofi_threshold:
                self._state = self._STATE_COOLDOWN
                self._cooldown_start = now
                logger.info(
                    f"BULLISH SWEEP CONFIRMED at {self._current_price:.2f} "
                    f"(nOFI={nofi:+.2f}) → SELL"
                )
                return ("sell", "HIGH")
            return None

        # --- SCANNING: look for a new sweep ---
        if self._current_price < self._local_low - buf:
            self._state = self._STATE_SWEEP_LOW
            self._sweep_start_time = now
            logger.info(
                f"Sweep LOW triggered — price {self._current_price:.2f} "
                f"crossed below {self._local_low:.2f} (buf={buf:.2f})"
            )
        elif self._current_price > self._local_high + buf:
            self._state = self._STATE_SWEEP_HIGH
            self._sweep_start_time = now
            logger.info(
                f"Sweep HIGH triggered — price {self._current_price:.2f} "
                f"crossed above {self._local_high:.2f} (buf={buf:.2f})"
            )

        return None

    # -- dashboard info ----------------------------------------------------

    def level_info(self) -> LevelInfo:
        """Current level distances for the pulse dashboard."""
        price = self._current_price
        if price == 0.0 or self._local_high == 0.0:
            return LevelInfo(sweep_status=self._state)

        high_dist = (self._local_high - price) / price * 100
        low_dist = (price - self._local_low) / price * 100

        return LevelInfo(
            nearest_high=round(self._local_high, 2),
            nearest_low=round(self._local_low, 2),
            high_distance_pct=round(high_dist, 2),
            low_distance_pct=round(low_dist, 2),
            atr=round(self._atr, 2),
            sweep_status=self._state,
            current_price=round(price, 2),
        )
