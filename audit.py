"""What the schedule really saved (no Home Assistant imports).

After a day is over, its real consumption and PV production are run through
"standard" operation and compared with what the grid really cost. On days
the schedule left the battery alone the two should match; what they differ
by on such days is the error of the model and is taken off the other days.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from .optimizer import Battery, Slot, standard_day

WINDOW_DAYS = 14  # days the result covers
BIAS_DAYS = 28  # days the model error is taken from
MIN_ACTIVE_DAYS = 3  # days with a deviation needed before the result counts


def day_result(
    home: Sequence[float | None],
    solar: Sequence[float | None],
    grid: Sequence[float | None],
    export: Sequence[float | None],
    prices: Sequence[float | None],
    soc_start: float,
    soc_end: float,
    bat: Battery,
) -> float | None:
    """EUR the day cost less than "standard" would have; None with gaps in the data."""
    if any(v is None for series in (home, grid, prices) for v in series):
        return None
    slots = [
        Slot(hour * 3600.0, 1.0, prices[hour], home[hour], solar[hour] or 0.0)
        for hour in range(24)
    ]
    standard_cost, standard_soc = standard_day(slots, soc_start, bat)
    real_cost = sum(g * p for g, p in zip(grid, prices, strict=True)) - bat.export_price * sum(
        v or 0.0 for v in export
    )
    # Energy left in the battery is worth the day's cheapest price.
    worth = max(min(prices), 0.0) * bat.capacity / 100
    floor = bat.floor
    return (standard_cost - worth * max(standard_soc - floor, 0.0)) - (
        real_cost - worth * max(soc_end - floor, 0.0)
    )


@dataclass(frozen=True)
class Audit:
    """Result over the last days."""

    total: float  # EUR saved on days with a deviation, model error taken off
    active_days: int
    bias: float  # EUR per day the model is off on days without a deviation
    days: dict[str, float]  # ISO day -> EUR saved (days with a deviation)

    @property
    def losing(self) -> bool:
        """The schedule cost money over enough days to tell."""
        return self.active_days >= MIN_ACTIVE_DAYS and self.total < 0


def summarize(results: Sequence[tuple[date, float, bool]], today: date) -> Audit:
    """results: (day, EUR from day_result, schedule deviated from standard)."""
    quiet = [
        saved
        for day, saved, active in results
        if not active and 0 < (today - day).days <= BIAS_DAYS
    ]
    bias = sum(quiet) / len(quiet) if quiet else 0.0
    start = today - timedelta(days=WINDOW_DAYS)
    days = {
        day.isoformat(): round(saved - bias, 3)
        for day, saved, active in results
        if active and start <= day < today
    }
    return Audit(round(sum(days.values()), 2), len(days), round(bias, 3), days)
