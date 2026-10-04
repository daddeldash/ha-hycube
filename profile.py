"""Consumption profile and forecast (no Home Assistant imports).

The profile is a weekday x hour matrix of the average household consumption
in kWh, built from the hourly long-term statistics of the home energy sensor.
Recent weeks weigh more (exponential decay) so the profile follows seasons.
Slots without enough data fall back to the all-days average of that hour.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

HALF_LIFE_DAYS = 14.0
MIN_WEIGHT = 0.5  # below this summed weight a weekday slot uses the hour fallback


@dataclass
class ConsumptionProfile:
    """Average kWh per (weekday, hour) with an hour-of-day fallback."""

    slots: dict[tuple[int, int], float] = field(default_factory=dict)
    hourly: dict[int, float] = field(default_factory=dict)
    days_of_data: float = 0.0
    built: datetime | None = None

    @classmethod
    def build(
        cls, rows: Iterable[tuple[datetime, float]], now: datetime
    ) -> ConsumptionProfile:
        """rows: (local start of hour, kWh consumed in that hour)."""
        acc: dict[tuple[int, int], list[float]] = {}
        acc_h: dict[int, list[float]] = {}
        hours = 0
        for start, kwh in rows:
            if kwh is None or kwh < 0 or math.isnan(kwh):
                continue  # counter resets or corrupt hours
            age_days = max((now - start).total_seconds() / 86400, 0.0)
            weight = 0.5 ** (age_days / HALF_LIFE_DAYS)
            for store, key in ((acc, (start.weekday(), start.hour)), (acc_h, start.hour)):
                w_sum, v_sum = store.setdefault(key, [0.0, 0.0])
                store[key] = [w_sum + weight, v_sum + weight * kwh]
            hours += 1
        slots = {k: v / w for k, (w, v) in acc.items() if w >= MIN_WEIGHT}
        hourly = {k: v / w for k, (w, v) in acc_h.items() if w > 0}
        return cls(slots=slots, hourly=hourly, days_of_data=hours / 24, built=now)

    @property
    def ready(self) -> bool:
        """At least one full day of data."""
        return len(self.hourly) == 24

    def expected(self, start: datetime) -> float | None:
        """Expected kWh for the hour starting at `start` (local time)."""
        val = self.slots.get((start.weekday(), start.hour))
        if val is None:
            val = self.hourly.get(start.hour)
        return val

    def forecast(self, now: datetime, until: datetime) -> float | None:
        """Expected kWh from now until `until` (partial hours pro rata)."""
        if not self.ready or until <= now:
            return 0.0 if self.ready else None
        total = 0.0
        cursor = now
        while cursor < until:
            hour_start = cursor.replace(minute=0, second=0, microsecond=0)
            hour_end = hour_start + timedelta(hours=1)
            seg_end = min(hour_end, until)
            frac = (seg_end - cursor).total_seconds() / 3600
            total += (self.expected(hour_start) or 0.0) * frac
            cursor = seg_end
        return total

    def hourly_forecast(self, now: datetime, hours: int = 24) -> list[dict]:
        """Next `hours` full hours as [{start, kwh}], for charts/automations."""
        if not self.ready:
            return []
        first = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        out = []
        for i in range(hours):
            start = first + timedelta(hours=i)
            out.append({"start": start.isoformat(), "kwh": round(self.expected(start) or 0.0, 3)})
        return out

    def day_profile(self, weekday: int) -> list[float]:
        """24 hourly kWh values for a weekday (0 = Monday)."""
        return [
            round(self.slots.get((weekday, h), self.hourly.get(h, 0.0)), 3)
            for h in range(24)
        ]

    def as_dict(self) -> dict:
        """Serialisable form for the HA Store."""
        return {
            "slots": {f"{d}-{h}": v for (d, h), v in self.slots.items()},
            "hourly": {str(h): v for h, v in self.hourly.items()},
            "days_of_data": self.days_of_data,
            "built": self.built.isoformat() if self.built else None,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> ConsumptionProfile:
        """Inverse of as_dict()."""
        if not data:
            return cls()
        slots = {}
        for key, val in data.get("slots", {}).items():
            d, h = key.split("-")
            slots[(int(d), int(h))] = float(val)
        built = data.get("built")
        return cls(
            slots=slots,
            hourly={int(h): float(v) for h, v in data.get("hourly", {}).items()},
            days_of_data=float(data.get("days_of_data", 0)),
            built=datetime.fromisoformat(built) if built else None,
        )
