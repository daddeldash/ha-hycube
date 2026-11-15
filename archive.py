"""Permanent hourly archive of consumption and PV production (no Home Assistant imports).

One record per local day, kept for good so the consumption model can learn
from months and years:

  home    24 hourly kWh the household consumed
  solar   24 hourly kWh the PV produced
  grid    24 hourly kWh taken from the grid
  export  24 hourly kWh fed into the grid
  temp    mean outdoor temperature of the day, deg C
  pred    consumption forecast as it stood at the start of the day
  pv_fc   PV forecast as it stood at the start of the day
  price   24 hourly grid prices, EUR/kWh
  soc     % state of charge at the start of the day
  active  the schedule put the battery into "hold" or "grid charge"
  saved   EUR the day cost less than "standard" would have (see audit.py)

`pred`, `pv_fc` and `price` only serve to judge the forecasts and the
schedule and are dropped after SNAPSHOT_DAYS. A day the device had no data for is marked `empty` so
it is not asked for again.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any

HOME = "home"
SOLAR = "solar"
TEMP = "temp"
PRED = "pred"
PV_FORECAST = "pv_fc"
GRID = "grid"
EXPORT = "export"
PRICE = "price"
SOC = "soc"
ACTIVE = "active"
SAVED = "saved"
EMPTY = "empty"

SNAPSHOT_DAYS = 120
# The device has no older data once this many days in a row came back empty.
EMPTY_DAYS_END = 14
# How far back the archive ever looks.
MAX_AGE_DAYS = 3660

Hours = Sequence[float | None]


def _clean(hours: Hours | None) -> list[float | None] | None:
    """24 rounded values, or None if there is nothing in them."""
    if not hours or all(v is None for v in hours):
        return None
    values = [None if v is None else round(float(v), 4) for v in hours][:24]
    return values + [None] * (24 - len(values))


class Archive:
    """Day records keyed by ISO date."""

    def __init__(self, data: Mapping[str, Any] | None = None) -> None:
        data = data or {}
        self.days: dict[str, dict[str, Any]] = dict(data.get("days") or {})
        # The device has nothing older than the oldest record.
        self.complete: bool = bool(data.get("complete", False))
        self.dirty = False

    def as_dict(self) -> dict[str, Any]:
        """Serialisable form for the store."""
        return {"days": self.days, "complete": self.complete}

    @classmethod
    def from_legacy(cls, history: Mapping[str, Any] | None) -> Archive:
        """Take over the cache of earlier versions: {day: 24 kWh or None}."""
        archive = cls()
        for day, hours in (history or {}).items():
            try:
                archive.set_energy(date.fromisoformat(day), hours, None)
            except (TypeError, ValueError):
                continue
        return archive

    # --- energy -------------------------------------------------------------------
    def known(self, day: date) -> bool:
        """The day's energy was fetched before, with or without data."""
        record = self.days.get(day.isoformat(), {})
        return HOME in record or SOLAR in record or EMPTY in record

    def set_energy(
        self,
        day: date,
        home: Hours | None,
        solar: Hours | None,
        grid: Hours | None = None,
        export: Hours | None = None,
    ) -> None:
        """Store what the device returned for a day; None means no data."""
        record = self.days.setdefault(day.isoformat(), {})
        for key, hours in ((HOME, home), (SOLAR, solar), (GRID, grid), (EXPORT, export)):
            if (values := _clean(hours)) is not None:
                record[key] = values
        if HOME in record or SOLAR in record:
            record.pop(EMPTY, None)
        else:
            record[EMPTY] = True
        self.dirty = True

    def pending(self, today: date, limit: int) -> list[date]:
        """Days still to fetch, newest first: gaps, then further into the past."""
        oldest = self._oldest_fetched()
        days: list[date] = []
        for age in range(1, MAX_AGE_DAYS + 1):
            day = today - timedelta(days=age)
            if self.complete and (oldest is None or day < oldest):
                break
            if not self.known(day):
                days.append(day)
                if len(days) >= limit:
                    break
        return days

    def check_complete(self) -> None:
        """Stop looking further back once the oldest days are all empty."""
        fetched = sorted(day for day in self.days if self.known(date.fromisoformat(day)))
        oldest = fetched[:EMPTY_DAYS_END]
        if (
            not self.complete
            and len(oldest) == EMPTY_DAYS_END
            and all(EMPTY in self.days[day] for day in oldest)
        ):
            self.complete = True
            self.dirty = True

    def _oldest_fetched(self) -> date | None:
        days = [day for day in self.days if self.known(date.fromisoformat(day))]
        return date.fromisoformat(min(days)) if days else None

    def oldest(self) -> date | None:
        """Oldest day with consumption data."""
        days = [day for day, record in self.days.items() if HOME in record]
        return date.fromisoformat(min(days)) if days else None

    def count(self) -> int:
        """Days with consumption data."""
        return sum(1 for record in self.days.values() if HOME in record)

    def records(self, key: str = HOME) -> Iterator[tuple[date, list[float | None], dict[str, Any]]]:
        """(day, 24 values, whole record) of all days that have `key`, oldest first."""
        for day in sorted(self.days):
            record = self.days[day]
            if key in record:
                yield date.fromisoformat(day), record[key], record

    def rows(self, tz: tzinfo, key: str = HOME) -> list[tuple[datetime, float]]:
        """(local start of hour, kWh) of everything stored."""
        out = []
        for day, hours, _ in self.records(key):
            start = datetime.combine(day, time.min, tzinfo=tz)
            out += [
                (start + timedelta(hours=h), value)
                for h, value in enumerate(hours)
                if value is not None
            ]
        return out

    # --- temperature ----------------------------------------------------------------
    def temp(self, day: date) -> float | None:
        """Mean outdoor temperature of a day."""
        return self.days.get(day.isoformat(), {}).get(TEMP)

    def set_temp(self, day: date, value: float) -> None:
        record = self.days.setdefault(day.isoformat(), {})
        value = round(float(value), 2)
        if record.get(TEMP) != value:
            record[TEMP] = value
            self.dirty = True

    def days_without_temp(self) -> list[date]:
        """Days with consumption but no temperature yet, oldest first."""
        return [day for day, _, record in self.records() if TEMP not in record]

    # --- forecast snapshots -----------------------------------------------------------
    def snapshot(self, day: date, key: str) -> list[float | None] | None:
        return self.days.get(day.isoformat(), {}).get(key)

    def set_snapshot(self, day: date, key: str, hours: Hours) -> bool:
        """Remember a forecast for a day; the first one of the day stays."""
        record = self.days.setdefault(day.isoformat(), {})
        if key in record or (values := _clean(hours)) is None:
            return False
        record[key] = values
        self.dirty = True
        return True

    def prune_snapshots(self, today: date) -> None:
        """Drop forecasts that are too old to matter for the error figures."""
        limit = (today - timedelta(days=SNAPSHOT_DAYS)).isoformat()
        for day, record in self.days.items():
            if day < limit and any(key in record for key in (PRED, PV_FORECAST, PRICE)):
                for key in (PRED, PV_FORECAST, PRICE):
                    record.pop(key, None)
                self.dirty = True

    # --- single values ------------------------------------------------------------------
    def value(self, day: date, key: str) -> Any:
        return self.days.get(day.isoformat(), {}).get(key)

    def set_value(self, day: date, key: str, value: Any) -> None:
        record = self.days.setdefault(day.isoformat(), {})
        if record.get(key) != value:
            record[key] = value
            self.dirty = True

    def pairs(self, actual: str, forecast: str) -> list[tuple[date, list, list]]:
        """(day, actual hours, forecast hours) where both exist, oldest first."""
        return [
            (day, hours, record[forecast])
            for day, hours, record in self.records(actual)
            if forecast in record
        ]
