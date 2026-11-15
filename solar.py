"""Correction of the PV forecast from what the plant really produced (no Home Assistant imports).

Forecast services do not know shading, dirt or the exact orientation. The
ratio of real to forecast production over the last weeks corrects that, per
hour of the day so a shaded morning or evening is covered too.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

HALF_LIFE = 30.0  # days
MIN_DAYS = 5
MIN_DAY_FORECAST = 0.3  # kWh a day must be forecast to count
PRIOR = 1.0  # kWh of forecast that pull an hour towards the overall factor
LOWEST, HIGHEST = 0.3, 1.5


@dataclass(frozen=True)
class PvCorrection:
    """Factors to multiply the forecast with."""

    overall: float
    hourly: tuple[float, ...]  # 24, by local hour
    days: int

    def factor(self, hour: int) -> float:
        return self.hourly[hour % 24]


def _clamp(value: float) -> float:
    return min(max(value, LOWEST), HIGHEST)


def fit_correction(
    pairs: Sequence[tuple[date, Sequence[float | None], Sequence[float | None]]],
    today: date,
) -> PvCorrection | None:
    """pairs: (day, produced kWh per hour, forecast kWh per hour); None with too little data."""
    produced = [0.0] * 24
    forecast = [0.0] * 24
    days = 0
    for day, actual, expected in pairs:
        age = (today - day).days
        if age <= 0 or sum(v or 0.0 for v in expected) < MIN_DAY_FORECAST:
            continue
        weight = 0.5 ** (age / HALF_LIFE)
        days += 1
        for h in range(24):
            if actual[h] is None or expected[h] is None:
                continue
            produced[h] += weight * actual[h]
            forecast[h] += weight * expected[h]
    if days < MIN_DAYS or sum(forecast) <= 0:
        return None
    overall = _clamp(sum(produced) / sum(forecast))
    hourly = tuple(
        _clamp((produced[h] + PRIOR * overall) / (forecast[h] + PRIOR)) for h in range(24)
    )
    return PvCorrection(round(overall, 3), tuple(round(f, 3) for f in hourly), days)
