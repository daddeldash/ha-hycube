"""Energy accounting from power samples (no Home Assistant imports).

The HyCube exposes power only, no energy counters. Energy is integrated with
the trapezoid rule from the realtime samples; gaps are filled from the
5-minute statistics of /db_today/ so a network outage does not lose energy.

Sign conventions (checked on the device: Home_P == Inv1_P + Grid_P):
  Grid_P    > 0 import from grid, < 0 export to grid
  Battery_P > 0 charging,         < 0 discharging
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any

from .const import (
    ENERGY_BATTERY_CHARGE,
    ENERGY_BATTERY_DISCHARGE,
    ENERGY_EXTERNAL,
    ENERGY_GRID_EXPORT,
    ENERGY_GRID_IMPORT,
    ENERGY_HOME,
    ENERGY_KEYS,
    ENERGY_SOLAR,
)

Sample = tuple[float, dict[str, float]]  # (unix timestamp, W per energy key)
# 5-minute bucket of /db_today/: (start ts, end ts, average W per energy key)
Bucket = tuple[float, float, dict[str, float]]


def to_float(value: Any) -> float | None:
    """Parse numbers that may arrive as strings ("-846.5")."""
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _get(data: Mapping[str, Any], *keys: str) -> float:
    """First parseable value of several spellings (the firmware mixes case)."""
    for key in keys:
        if key in data and (val := to_float(data[key])) is not None:
            return val
    return 0.0


def _pos(value: float) -> float:
    return value if value > 0 else 0.0


def _split(grid: float, battery: float, solar: float, external: float, home: float):
    return {
        ENERGY_GRID_IMPORT: _pos(grid),
        ENERGY_GRID_EXPORT: _pos(-grid),
        ENERGY_BATTERY_CHARGE: _pos(battery),
        ENERGY_BATTERY_DISCHARGE: _pos(-battery),
        ENERGY_SOLAR: max(solar, 0.0),
        ENERGY_EXTERNAL: max(external, 0.0),
        ENERGY_HOME: max(home, 0.0),
    }


def powers_from_values(values: Mapping[str, Any]) -> dict[str, float]:
    """Split a /get_values/ response into the unsigned energy flows."""
    solar = _get(values, "solar_total_P")
    if not solar:
        solar = sum(
            _get(values, f"solar{i}_p", f"solar{i}_P", f"Solar{i}_P") for i in range(1, 5)
        )
    return _split(
        grid=_get(values, "Grid_P"),
        battery=_get(values, "Battery_P"),
        solar=solar,
        external=_get(values, "Meter2_P"),
        home=_get(values, "Home_P"),
    )


def buckets_from_day_stats(
    data: Mapping[str, Any], day: date, tz: tzinfo
) -> list[Bucket]:
    """Parse /db_today/?day=YYYY-MM-DD (HyWeb statistics chart).

    Every list holds 5-minute average powers in W; "Zeitstempel" is the end
    of each interval. The categories are unsigned flows, but the firmware
    reports some of them negative (e.g. Netzbezug), so abs() is used.
    """
    stamps = data.get("Zeitstempel")
    if not isinstance(stamps, list):
        return []

    def col(key: str, i: int) -> float:
        values = data.get(key)
        if not isinstance(values, list) or i >= len(values):
            return 0.0
        val = to_float(values[i])
        return abs(val) if val is not None else 0.0

    buckets: list[Bucket] = []
    day_offset = 0
    prev: time | None = None
    for i, stamp in enumerate(stamps):
        try:
            t = datetime.strptime(str(stamp), "%H:%M:%S").time()
        except ValueError:
            continue
        if prev is not None and t <= prev:
            day_offset = 1  # "00:00:00" closes the last interval of the day
        prev = t
        end = datetime.combine(day + timedelta(days=day_offset), t, tzinfo=tz)
        start = end - timedelta(minutes=5)
        solar = sum(col(f"solar{n}", i) for n in range(1, 5)) or col("total", i)
        buckets.append(
            (
                start.timestamp(),
                end.timestamp(),
                {
                    ENERGY_GRID_IMPORT: col("Netzbezug", i),
                    ENERGY_GRID_EXPORT: col("Einspeisung", i),
                    ENERGY_BATTERY_CHARGE: col("Batterieaufladung", i),
                    ENERGY_BATTERY_DISCHARGE: col("batterieentladung", i),
                    ENERGY_SOLAR: solar,
                    ENERGY_EXTERNAL: col("mtr2", i),
                    ENERGY_HOME: col("hausverbrauch", i),
                },
            )
        )
    return buckets


def hourly_kwh(buckets: Iterable[Bucket], key: str, tz: tzinfo) -> dict[datetime, float]:
    """Sum buckets into local hours; hours with < 10 of 12 buckets are dropped."""
    acc: dict[datetime, list[float]] = {}
    for start, end, powers in buckets:
        hour = datetime.fromtimestamp(start, tz).replace(minute=0, second=0, microsecond=0)
        wh_n = acc.setdefault(hour, [0.0, 0])
        wh_n[0] += powers.get(key, 0.0) * (end - start) / 3600
        wh_n[1] += 1
    return {h: wh / 1000 * 12 / n for h, (wh, n) in acc.items() if n >= 10}


class EnergyAccumulator:
    """Running energy totals in Wh, integrated with the trapezoid rule."""

    def __init__(self, totals: Mapping[str, float] | None = None) -> None:
        self.totals: dict[str, float] = {k: 0.0 for k in ENERGY_KEYS}
        if totals:
            for key, val in totals.items():
                if key in self.totals and (num := to_float(val)) is not None:
                    self.totals[key] = num
        self.last: Sample | None = None

    def integrate(self, start: Sample, end: Sample) -> dict[str, float]:
        """Add the energy between two samples; returns the added Wh."""
        dt_h = (end[0] - start[0]) / 3600
        if dt_h <= 0:
            return {}
        added = {}
        for key in ENERGY_KEYS:
            wh = (start[1].get(key, 0.0) + end[1].get(key, 0.0)) / 2 * dt_h
            self.totals[key] += wh
            added[key] = wh
        return added

    def add_buckets(self, buckets: Iterable[Bucket], t0: float, t1: float) -> float:
        """Add the share of each bucket that lies inside (t0, t1].

        Returns the number of seconds of (t0, t1] covered by buckets.
        """
        covered = 0.0
        for start, end, powers in buckets:
            overlap = min(end, t1) - max(start, t0)
            if overlap <= 0:
                continue
            covered += overlap
            for key in ENERGY_KEYS:
                self.totals[key] += powers.get(key, 0.0) * overlap / 3600
        return covered

    def add_series(
        self, samples: Iterable[Sample], max_gap: float | None = None
    ) -> float:
        """Integrate an ordered series continuing from self.last.

        Segments longer than max_gap seconds are not integrated (no data to
        trust); returns the number of seconds skipped that way.
        """
        skipped = 0.0
        for sample in sorted(samples, key=lambda s: s[0]):
            if self.last is not None and sample[0] <= self.last[0]:
                continue
            if self.last is not None:
                gap = sample[0] - self.last[0]
                if max_gap is not None and gap > max_gap:
                    skipped += gap
                else:
                    self.integrate(self.last, sample)
            self.last = sample
        return skipped
