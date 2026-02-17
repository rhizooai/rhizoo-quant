from __future__ import annotations

import os
import time
from collections import deque
from dataclasses import dataclass, field
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
    candle_window: int = Field(default=240, description="Rolling candle count (4h of 1m candles)")
    h1_lookback: int = Field(default=60, description="Candles for H1 extremes")
    atr_period: int = Field(default=14, description="ATR lookback period")
    buffer_zone_pct: float = Field(default=0.0005, description="0.05% buffer around levels")
    nofi_threshold: float = Field(default=0.7, description="Minimum |nOFI| to confirm a sweep")
    sweep_timeout_sec: float = Field(default=60.0, description="Max time in SWEEPING state")
    confirm_timeout_sec: float = Field(default=30.0, description="Max time in CONFIRMING state")
    cooldown_sec: float = Field(default=1800.0, description="Per-level cooldown after signal (30 min)")


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
    h1_high: float = Field(default=0.0)
    h1_low: float = Field(default=0.0)
    h4_high: float = Field(default=0.0)
    h4_low: float = Field(default=0.0)
    h1_high_dist_pct: float = Field(default=0.0)
    h1_low_dist_pct: float = Field(default=0.0)
    h4_high_dist_pct: float = Field(default=0.0)
    h4_low_dist_pct: float = Field(default=0.0)
    atr: float = Field(default=0.0)
    current_price: float = Field(default=0.0)
    near_liquidity: str = Field(default="NONE", description="Level name we're near, or NONE")
    hunt_summary: str = Field(default="SCANNING", description="Aggregate hunt status")


class SweepResult(BaseModel):
    """Emitted when a sweep is confirmed."""
    side: str = Field(..., description="'buy' or 'sell'")
    strength: str = Field(default="HIGH")
    level_name: str = Field(..., description="e.g. 'H4_Low'")
    level_price: float
    wick_extreme: float = Field(..., description="Furthest sweep point (the stop-loss)")
    fib_tp: float = Field(..., description="0.5 Fibonacci TP of the range")
    range_high: float
    range_low: float


# ---------------------------------------------------------------------------
# ImbalanceTracker
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
# LevelTracker — multi-timeframe liquidity levels + Hunter state machine
# ---------------------------------------------------------------------------

# Candle columns: [open, high, low, close, volume, timestamp_ms_open]
_O, _H, _L, _C, _V, _T = 0, 1, 2, 3, 4, 5

# Hunt states
_SCANNING = "SCANNING"
_SWEEPING = "SWEEPING"
_CONFIRMING = "CONFIRMING"
_COOLDOWN = "COOLDOWN"


@dataclass
class _HuntState:
    """Per-level sweep state machine."""
    name: str
    is_high: bool  # True = tracking a high level, False = low level
    state: str = _SCANNING
    level_price: float = 0.0
    opposite_price: float = 0.0
    wick_extreme: float = 0.0
    sweep_start: float = 0.0
    cooldown_until: float = 0.0


class LevelTracker:
    """Multi-timeframe liquidity level tracker with per-level Hunter state machines.

    Synthesises 1-min candles from the trade stream, computes H1/H4 extremes,
    and runs SCANNING → SWEEPING → CONFIRMING per level.
    """

    def __init__(self, config: LevelConfig | None = None) -> None:
        self.config = config or LevelConfig()

        self._candles: deque[np.ndarray] = deque(maxlen=self.config.candle_window)
        self._current_candle: np.ndarray | None = None
        self._candle_open_ts: float = 0.0

        self._current_price: float = 0.0
        self._atr: float = 0.0

        # Multi-timeframe levels
        self._h1_high: float = 0.0
        self._h1_low: float = 0.0
        self._h4_high: float = 0.0
        self._h4_low: float = 0.0

        # Per-level hunt states
        self._hunts: dict[str, _HuntState] = {
            "H1_High": _HuntState(name="H1_High", is_high=True),
            "H1_Low":  _HuntState(name="H1_Low",  is_high=False),
            "H4_High": _HuntState(name="H4_High", is_high=True),
            "H4_Low":  _HuntState(name="H4_Low",  is_high=False),
        }

        logger.info(
            f"LevelTracker initialized "
            f"(window={self.config.candle_window} candles, "
            f"H1={self.config.h1_lookback}, "
            f"buffer={self.config.buffer_zone_pct:.4%}, "
            f"cooldown={self.config.cooldown_sec}s)"
        )

    # -- candle synthesis --------------------------------------------------

    def _bucket_ts(self, ts_ms: float) -> float:
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
            if self._current_candle is not None:
                self._candles.append(self._current_candle.copy())
                self._recompute_levels()
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
        """Recompute H1/H4 extremes and ATR from finalised candles."""
        n = len(self._candles)
        if n < 2:
            return

        all_candles = np.array(self._candles, dtype=np.float64)

        # H4 = full window
        self._h4_high = float(np.max(all_candles[:, _H]))
        self._h4_low = float(np.min(all_candles[:, _L]))

        # H1 = last h1_lookback candles
        h1_slice = all_candles[-min(self.config.h1_lookback, n):]
        self._h1_high = float(np.max(h1_slice[:, _H]))
        self._h1_low = float(np.min(h1_slice[:, _L]))

        # Update hunt level prices (only for SCANNING state — don't move active hunts)
        level_map = {
            "H1_High": (self._h1_high, self._h1_low),
            "H1_Low":  (self._h1_low, self._h1_high),
            "H4_High": (self._h4_high, self._h4_low),
            "H4_Low":  (self._h4_low, self._h4_high),
        }
        for name, (lvl, opp) in level_map.items():
            h = self._hunts[name]
            if h.state == _SCANNING:
                h.level_price = lvl
                h.opposite_price = opp

        # ATR
        atr_n = min(self.config.atr_period, n)
        recent = all_candles[-atr_n:]
        if atr_n >= 2:
            h = recent[:, _H]
            l = recent[:, _L]
            prev_c = np.empty_like(h)
            prev_c[0] = recent[0, _O]
            prev_c[1:] = recent[:-1, _C]
            tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
            self._atr = float(np.mean(tr))

    # -- Hunter state machine (per-level) ----------------------------------

    def check_hunt(self, nofi: float) -> SweepResult | None:
        """Run the Hunter state machine for all tracked levels.

        Returns a SweepResult on the first confirmed signal, or None.
        """
        if self._current_price == 0.0:
            return None

        now = time.time()
        price = self._current_price
        buf = price * self.config.buffer_zone_pct

        for h in self._hunts.values():
            if h.level_price == 0.0:
                continue

            result = self._tick_hunt(h, price, buf, nofi, now)
            if result is not None:
                return result

        return None

    def _tick_hunt(
        self,
        h: _HuntState,
        price: float,
        buf: float,
        nofi: float,
        now: float,
    ) -> SweepResult | None:

        # --- COOLDOWN ---
        if h.state == _COOLDOWN:
            if now >= h.cooldown_until:
                h.state = _SCANNING
                logger.debug(f"[HUNT] {h.name} cooldown expired — SCANNING")
            return None

        # --- CONFIRMING ---
        if h.state == _CONFIRMING:
            elapsed = now - h.sweep_start
            total_timeout = self.config.sweep_timeout_sec + self.config.confirm_timeout_sec
            if elapsed > total_timeout:
                logger.debug(f"[HUNT] {h.name} CONFIRMING timed out — SCANNING")
                h.state = _SCANNING
                return None

            # Check if price left the zone again → back to SWEEPING
            if h.is_high and price > h.level_price + buf:
                h.state = _SWEEPING
                h.wick_extreme = max(h.wick_extreme, price)
                return None
            if not h.is_high and price < h.level_price - buf:
                h.state = _SWEEPING
                h.wick_extreme = min(h.wick_extreme, price)
                return None

            # Check nOFI confirmation
            if h.is_high and nofi <= -self.config.nofi_threshold:
                # Bullish sweep confirmed → SELL
                h.state = _COOLDOWN
                h.cooldown_until = now + self.config.cooldown_sec
                logger.info(
                    f"[HUNT] CONFIRMED {h.name} sweep at {price:.2f} "
                    f"(nOFI={nofi:+.2f}) → SELL"
                )
                return self._build_result(h, "sell", price)

            if not h.is_high and nofi >= self.config.nofi_threshold:
                # Bearish sweep confirmed → BUY
                h.state = _COOLDOWN
                h.cooldown_until = now + self.config.cooldown_sec
                logger.info(
                    f"[HUNT] CONFIRMED {h.name} sweep at {price:.2f} "
                    f"(nOFI={nofi:+.2f}) → BUY"
                )
                return self._build_result(h, "buy", price)

            return None

        # --- SWEEPING ---
        if h.state == _SWEEPING:
            elapsed = now - h.sweep_start
            if elapsed > self.config.sweep_timeout_sec:
                logger.debug(f"[HUNT] {h.name} SWEEPING timed out — SCANNING")
                h.state = _SCANNING
                return None

            # Track wick extreme
            if h.is_high:
                h.wick_extreme = max(h.wick_extreme, price)
            else:
                h.wick_extreme = min(h.wick_extreme, price)

            # Check snap-back: price returned inside the level
            if h.is_high and price < h.level_price:
                h.state = _CONFIRMING
                logger.info(
                    f"[HUNT] {h.name} price snapped back inside ({price:.2f} < "
                    f"{h.level_price:.2f}). Waiting for Imbalance..."
                )
            elif not h.is_high and price > h.level_price:
                h.state = _CONFIRMING
                logger.info(
                    f"[HUNT] {h.name} price snapped back inside ({price:.2f} > "
                    f"{h.level_price:.2f}). Waiting for Imbalance..."
                )

            return None

        # --- SCANNING ---
        if h.is_high and price > h.level_price + buf:
            h.state = _SWEEPING
            h.sweep_start = now
            h.wick_extreme = price
            logger.info(
                f"[HUNT] Potential Bullish sweep detected at {h.name} "
                f"({h.level_price:.2f}). Price={price:.2f}. Waiting for Imbalance..."
            )
        elif not h.is_high and price < h.level_price - buf:
            h.state = _SWEEPING
            h.sweep_start = now
            h.wick_extreme = price
            logger.info(
                f"[HUNT] Potential Bearish sweep detected at {h.name} "
                f"({h.level_price:.2f}). Price={price:.2f}. Waiting for Imbalance..."
            )

        return None

    def _build_result(self, h: _HuntState, side: str, price: float) -> SweepResult:
        """Build a SweepResult with Fibonacci TP."""
        range_high = max(h.level_price, h.opposite_price)
        range_low = min(h.level_price, h.opposite_price)
        fib_50 = range_low + 0.5 * (range_high - range_low)

        return SweepResult(
            side=side,
            strength="HIGH",
            level_name=h.name,
            level_price=round(h.level_price, 2),
            wick_extreme=round(h.wick_extreme, 2),
            fib_tp=round(fib_50, 2),
            range_high=round(range_high, 2),
            range_low=round(range_low, 2),
        )

    # -- dashboard info ----------------------------------------------------

    def _nearest_liquidity(self, price: float, buf: float) -> str:
        """Return the name of the nearest level within the buffer zone, or NONE."""
        levels = {
            "H4_High": self._h4_high,
            "H4_Low": self._h4_low,
            "H1_High": self._h1_high,
            "H1_Low": self._h1_low,
        }
        # Prefer H4 over H1 (higher-timeframe = stronger liquidity)
        for name, lvl in levels.items():
            if lvl > 0 and abs(price - lvl) <= buf:
                return name
        return "NONE"

    def _hunt_summary(self) -> str:
        """Aggregate hunt status for the dashboard."""
        active = [
            f"{h.name}:{h.state}"
            for h in self._hunts.values()
            if h.state not in (_SCANNING, _COOLDOWN)
        ]
        if active:
            return " | ".join(active)
        cooldowns = [h.name for h in self._hunts.values() if h.state == _COOLDOWN]
        if cooldowns:
            return f"COOLDOWN ({', '.join(cooldowns)})"
        return "SCANNING"

    def level_info(self) -> LevelInfo:
        """Current level distances for the pulse dashboard."""
        price = self._current_price
        if price == 0.0 or self._h4_high == 0.0:
            return LevelInfo(hunt_summary=self._hunt_summary())

        buf = price * self.config.buffer_zone_pct

        def dist_pct(level: float) -> float:
            return round((level - price) / price * 100, 3)

        return LevelInfo(
            h1_high=round(self._h1_high, 2),
            h1_low=round(self._h1_low, 2),
            h4_high=round(self._h4_high, 2),
            h4_low=round(self._h4_low, 2),
            h1_high_dist_pct=dist_pct(self._h1_high),
            h1_low_dist_pct=dist_pct(self._h1_low),
            h4_high_dist_pct=dist_pct(self._h4_high),
            h4_low_dist_pct=dist_pct(self._h4_low),
            atr=round(self._atr, 2),
            current_price=round(price, 2),
            near_liquidity=self._nearest_liquidity(price, buf),
            hunt_summary=self._hunt_summary(),
        )
