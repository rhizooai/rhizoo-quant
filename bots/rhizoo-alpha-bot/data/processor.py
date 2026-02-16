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


# ---------------------------------------------------------------------------
# Metrics model — passed to RiskManager / Strategy / Dashboard
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
        # Iterate from the right (newest) — deque is append-right
        rows: list[tuple[float, bool, float, float]] = []
        for entry in reversed(self._trades):
            if entry[0] < cutoff:
                break
            rows.append(entry)

        if not rows:
            return np.empty((0, 4), dtype=np.float64)

        arr = np.array(rows, dtype=np.float64)
        return arr[::-1]  # chronological order

    # -- core metrics ------------------------------------------------------

    def compute_nofi(self, window: np.ndarray) -> tuple[float, float, float]:
        """Normalised Order Flow Imbalance from a trade window.

        Returns (nOFI, buy_volume, sell_volume).
        """
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
        """Z-Score of current 1-minute volume vs the trailing 20-minute distribution.

        Buckets trades into 1-minute intervals, computes mean/std, then scores
        the most recent bucket.
        """
        window_sec = self.config.volume_window_min * 60
        arr = self._window(window_sec)
        if len(arr) == 0:
            return 0.0

        now_ms = time.time() * 1000
        bucket_ms = 60_000.0  # 1 minute

        timestamps = arr[:, 0]
        amounts = arr[:, 3]

        # Assign each trade to a 1-minute bucket index (0 = oldest)
        bucket_ids = ((timestamps - timestamps[0]) // bucket_ms).astype(np.int64)

        # Sum volume per bucket
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
