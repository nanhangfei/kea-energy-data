"""Dependency-free hardship signal arithmetic shared with the data pipeline.

The API's broader ``analytics.engine`` imports Pydantic response schemas. A
batch identification job needs none of that, and requiring the entire API
runtime merely to compare two usage averages makes both deployment and unit
testing needlessly fragile. The pure detector lives here; ``engine`` imports
and re-exports it so existing API callers keep the same public surface.

CARRIED HERE FROM THE APP. This is a copy of the app's
`services/api/app/analytics/hardship.py` (13 Oct 2026), so that this repository
publishes on its own. A change to either copy belongs in both.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


class UsageSeries(Protocol):
    daily_kwh: Sequence[float]


@dataclass(frozen=True)
class CollapseSignal:
    """A usage collapse — often someone rationing heat they cannot afford."""

    detected: bool
    earlier_mean: float
    recent_mean: float
    drop_pct: float


def detect_usage_collapse(
    window: UsageSeries | Sequence[float],
    tail_days: int = 7,
    threshold_pct: float = 40.0,
) -> CollapseSignal:
    """Compare the most recent week against the rest of the supplied period.

    Consumer Care Obligations cl. 9.3(c) names significant consumption
    changes as a possible payment-difficulty signal. This is a signal, never a
    diagnosis; a collapse might be a holiday and must route to human context.

    ``window`` accepts the API's ``UsageWindow`` or a plain sequence so the
    pipeline can reuse this arithmetic without importing API response models.
    """
    if tail_days <= 0:
        raise ValueError("tail_days must be positive")
    daily = list(window.daily_kwh) if hasattr(window, "daily_kwh") else list(window)
    if len(daily) <= tail_days:
        return CollapseSignal(False, 0.0, 0.0, 0.0)

    earlier = statistics.fmean(daily[:-tail_days])
    recent = statistics.fmean(daily[-tail_days:])
    drop = (earlier - recent) / earlier * 100 if earlier else 0.0
    return CollapseSignal(drop >= threshold_pct, earlier, recent, drop)
