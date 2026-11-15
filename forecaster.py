"""Consumption forecast that learns from the archive.

Once a night the models are tested against the archive and the best kind is
chosen; every hour the coming days are predicted with it, using the weather
forecast when the temperature model is in use. The result is handed to the
consumption profile, so the schedule and the forecast sensors use it as is.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util

from .archive import EXPORT, GRID, HOME, PRED, PRICE, PV_FORECAST, SAVED, SOC, SOLAR
from .audit import Audit, day_result, summarize
from .const import CONF_TEMPERATURE_ENTITY, CONF_WEATHER_ENTITY, DOMAIN
from .energy import to_float
from .model import (
    KINDS,
    RECENT,
    SEASONAL,
    TEMPERATURE,
    Backtest,
    Day,
    TemperatureFit,
    backtest,
    choose,
    fit_temperature,
    measured_error,
    predict,
)
from .profile import ConsumptionProfile
from .solar import PvCorrection, fit_correction

if TYPE_CHECKING:
    from .coordinator import HyCubeCoordinator

_LOGGER = logging.getLogger(__name__)

FORECAST_DAYS = 3  # today and the two days after
ERROR_DAYS = 14  # days the measured forecast error covers
# Hours of a day that need a temperature to take their mean.
MIN_TEMPERATURE_HOURS = 18
# Share of a new sample in the running offset between sensor and weather service.
OFFSET_WEIGHT = 1 / 24
RECORDER_YEARS = 10

_UNITS = {"temperature": UnitOfTemperature.CELSIUS}


def days_from_rows(
    rows: list[tuple[datetime, float]], temps: dict[date, float | None], before: date
) -> list[Day]:
    """Group (local hour start, kWh) into days older than `before`."""
    by_day: dict[date, list[float | None]] = {}
    for start, kwh in rows:
        if start.date() < before and kwh is not None and kwh >= 0:
            by_day.setdefault(start.date(), [None] * 24)[start.hour] = kwh
    return [Day(day, hours, temps.get(day)) for day, hours in sorted(by_day.items())]


class ConsumptionForecaster:
    """Chooses the consumption model and keeps the forecast current."""

    def __init__(self, hass: HomeAssistant, coordinator: HyCubeCoordinator) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.kind: str = RECENT  # kind of model in use
        self.backtest: Backtest | None = None
        self.fit: TemperatureFit | None = None
        self.offset: float | None = None  # sensor minus weather service, K
        self.temperatures: dict[date, float] = {}  # what the forecast is based on
        self.error: tuple[float, float, int] | None = None  # measured: daily, hourly, days
        self.pv: PvCorrection | None = None
        self.audit: Audit | None = None  # what the schedule really saved
        self._days: list[Day] = []
        self._lock = asyncio.Lock()

    # --- persistence ----------------------------------------------------------------
    def restore(self, data: dict[str, Any] | None) -> None:
        data = data or {}
        if data.get("kind") in KINDS:
            self.kind = data["kind"]
        self.offset = to_float(data.get("offset"))

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "offset": self.offset}

    # --- scheduling -------------------------------------------------------------------
    @callback
    def start(self) -> None:
        """Refresh the forecast shortly before every full hour."""
        self.coordinator.config_entry.async_on_unload(
            async_track_time_change(self.hass, self._scheduled_refresh, minute=57, second=0)
        )

    async def _scheduled_refresh(self, _now: datetime) -> None:
        await self.async_refresh()

    @property
    def _options(self) -> Any:
        return self.coordinator.config_entry.options

    # --- learning ---------------------------------------------------------------------
    async def async_rebuild(self, rows: list[tuple[datetime, float]]) -> None:
        """Learn from all hourly consumption there is, then refresh the forecast."""
        archive = self.coordinator.archive
        today = dt_util.now().date()
        async with self._lock:
            try:
                days = days_from_rows(rows, {}, today)
                await self._load_temperatures([d.date for d in days])
                days = [Day(d.date, d.hours, archive.temp(d.date)) for d in days]
                kinds = [k for k in KINDS if k != TEMPERATURE or self._temperature_configured()]
                self.backtest, self.kind, self.fit = await self.hass.async_add_executor_job(
                    _learn, days, kinds, today, self.kind
                )
                self._days = days
                self.error = measured_error(archive.pairs(HOME, PRED)[-ERROR_DAYS:])
                self.pv = fit_correction(archive.pairs(SOLAR, PV_FORECAST), today)
                await self._seed_peaks()
                self._audit(today)
                archive.prune_snapshots(today)
            except Exception:  # noqa: BLE001 - learning must never break the integration
                _LOGGER.exception("HyCube consumption model could not be rebuilt")
        _LOGGER.debug(
            "HyCube consumption model: %s (backtest %s)",
            self.kind,
            self.backtest.hourly if self.backtest else None,
        )
        await self.async_refresh()

    def _temperature_configured(self) -> bool:
        return bool(
            self._options.get(CONF_TEMPERATURE_ENTITY) and self._options.get(CONF_WEATHER_ENTITY)
        )

    def _audit(self, today: date) -> None:
        """Work out what past days really saved and whether the schedule pays."""
        coordinator = self.coordinator
        archive = coordinator.archive
        battery = coordinator.planner.battery_model()
        empty = [0.0] * 24
        for day, home, record in list(archive.records(HOME)):
            after = archive.value(day + timedelta(days=1), SOC)
            if (
                day >= today
                or SAVED in record
                or after is None
                or any(key not in record for key in (GRID, PRICE, SOC))
            ):
                continue
            saved = day_result(
                home,
                record.get(SOLAR) or empty,
                record[GRID],
                record.get(EXPORT) or empty,
                record[PRICE],
                record[SOC],
                after,
                battery,
            )
            if saved is not None:
                archive.set_value(day, SAVED, round(saved, 3))
        was_losing = self.audit is not None and self.audit.losing
        self.audit = summarize(
            [
                (day, saved, bool(record.get("active")))
                for day, saved, record in archive.records(SAVED)
            ],
            today,
        )
        if self.audit.losing and not was_losing:
            _LOGGER.warning(
                "HyCube schedule cost %.2f more than standard operation on %d days; "
                "it stays in 'standard' until those days are two weeks old",
                -self.audit.total,
                self.audit.active_days,
            )

    async def _seed_peaks(self) -> None:
        """Take the highest charge and discharge power from the sensors' statistics once."""
        coordinator = self.coordinator
        registry = er.async_get(self.hass)
        end = dt_util.now()
        for attr, key in (
            ("peak_charge", "battery_charge_power"),
            ("peak_discharge", "battery_discharge_power"),
        ):
            if getattr(coordinator, attr) is not None:
                continue
            entity_id = registry.async_get_entity_id(
                "sensor", DOMAIN, f"{coordinator.config_entry.unique_id}_{key}"
            )
            if not entity_id:
                continue
            rows = await self._statistics(
                entity_id, end - timedelta(days=90), end, "week", "max", None
            )
            if rows:
                setattr(coordinator, attr, max(value for _, value in rows))

    async def _statistics(
        self,
        entity_id: str,
        start: datetime,
        end: datetime,
        period: str,
        kind: str = "mean",
        units: dict[str, str] | None = _UNITS,
    ) -> list[tuple[datetime, float]]:
        """(local start, value) of a sensor's long-term statistics."""
        stats = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            dt_util.as_utc(start),
            dt_util.as_utc(end),
            {entity_id},
            period,
            units,
            {kind},
        )
        rows = []
        for row in stats.get(entity_id, []):
            begin, mean = row.get("start"), row.get(kind)
            if begin is None or mean is None:
                continue
            if isinstance(begin, (int, float)):
                begin = dt_util.utc_from_timestamp(begin)
            rows.append((dt_util.as_local(begin), float(mean)))
        return rows

    async def _load_temperatures(self, days: list[date]) -> None:
        """Store the mean outdoor temperature of archive days that lack it."""
        archive = self.coordinator.archive
        entity_id = self._options.get(CONF_TEMPERATURE_ENTITY)
        missing = [day for day in days if archive.temp(day) is None]
        if not entity_id or not missing:
            return
        today = dt_util.start_of_local_day()
        oldest = max(min(missing), today.date() - timedelta(days=RECORDER_YEARS * 366))
        start = datetime.combine(oldest, datetime.min.time(), tzinfo=today.tzinfo)
        wanted = set(missing)
        for begin, mean in await self._statistics(entity_id, start, today, "day"):
            if begin.date() in wanted:
                archive.set_temp(begin.date(), mean)

    # --- forecast -----------------------------------------------------------------------
    async def async_refresh(self) -> None:
        """Predict the coming days and hand the result to the profile."""
        coordinator = self.coordinator
        today = dt_util.now().date()
        async with self._lock:
            try:
                temperatures = await self._coming_temperatures(today)
                hours = await self.hass.async_add_executor_job(
                    _predict, self._days, self.kind, self.fit, today, temperatures
                )
            except Exception:  # noqa: BLE001 - keep the last forecast
                _LOGGER.exception("HyCube consumption forecast could not be refreshed")
                return
            self.temperatures = temperatures
            coordinator.profile.hours = hours
            own = [hours.get(ConsumptionProfile.hour_key(today, h)) for h in range(24)]
            if all(v is not None for v in own):
                coordinator.archive.set_snapshot(today, PRED, own)
        await coordinator.async_save_archive()
        coordinator.async_schedule_save()
        coordinator.async_update_listeners()
        coordinator.planner.schedule_update()

    async def _coming_temperatures(self, today: date) -> dict[date, float]:
        """Mean outdoor temperature of today and the next days, as the sensor would see it."""
        sensor = self._options.get(CONF_TEMPERATURE_ENTITY)
        weather = self._options.get(CONF_WEATHER_ENTITY)
        if not sensor or not weather:
            return {}
        self._update_offset(sensor, weather)
        offset = self.offset or 0.0

        by_hour: dict[datetime, float] = {}
        for item in await self._weather_forecast(weather, "hourly"):
            begin = dt_util.parse_datetime(str(item.get("datetime")))
            temp = to_float(item.get("temperature"))
            if begin is not None and temp is not None:
                hour = dt_util.as_local(begin).replace(minute=0, second=0, microsecond=0)
                by_hour[hour] = temp + offset
        # Hours of today that are over: what the sensor measured.
        midnight = dt_util.start_of_local_day()
        for begin, mean in await self._statistics(sensor, midnight, dt_util.now(), "hour"):
            by_hour[begin.replace(minute=0, second=0, microsecond=0)] = mean

        by_day: dict[date, float] = {}
        for item in await self._weather_forecast(weather, "daily"):
            begin = dt_util.parse_datetime(str(item.get("datetime")))
            high, low = to_float(item.get("temperature")), to_float(item.get("templow"))
            if begin is not None and high is not None and low is not None:
                by_day[dt_util.as_local(begin).date()] = (high + low) / 2 + offset

        result: dict[date, float] = {}
        for ahead in range(FORECAST_DAYS):
            day = today + timedelta(days=ahead)
            values = [t for hour, t in by_hour.items() if hour.date() == day]
            if len(values) >= MIN_TEMPERATURE_HOURS:
                result[day] = round(sum(values) / len(values), 2)
            elif day in by_day:
                result[day] = round(by_day[day], 2)
        return result

    def _update_offset(self, sensor: str, weather: str) -> None:
        """Track how far the outdoor sensor is from the weather service's temperature."""
        measured = self.hass.states.get(sensor)
        service = self.hass.states.get(weather)
        if measured is None or service is None:
            return
        here = to_float(measured.state)
        there = to_float(service.attributes.get("temperature"))
        if here is None or there is None:
            return
        diff = here - there
        self.offset = diff if self.offset is None else self.offset + OFFSET_WEIGHT * (diff - self.offset)

    async def _weather_forecast(self, entity_id: str, kind: str) -> list[dict[str, Any]]:
        try:
            response = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"entity_id": entity_id, "type": kind},
                blocking=True,
                return_response=True,
            )
        except (HomeAssistantError, ValueError, KeyError) as err:
            _LOGGER.debug("HyCube: no %s weather forecast from %s: %s", kind, entity_id, err)
            return []
        return list(((response or {}).get(entity_id) or {}).get("forecast") or [])


def _learn(
    days: list[Day], kinds: list[str], today: date, current: str
) -> tuple[Backtest, str, TemperatureFit | None]:
    """Test the models, pick one and fit the temperature model (runs in the executor)."""
    result = backtest(days, kinds)
    kind = choose(result.hourly, current if current in kinds else None)
    fit = fit_temperature(days, today) if TEMPERATURE in kinds else None
    return result, kind, fit


def _predict(
    days: list[Day],
    kind: str,
    fit: TemperatureFit | None,
    today: date,
    temperatures: dict[date, float],
) -> dict[str, float]:
    """Hourly kWh of the coming days by profile key (runs in the executor)."""
    hours: dict[str, float] = {}
    for ahead in range(FORECAST_DAYS):
        day = today + timedelta(days=ahead)
        values = None
        if kind == TEMPERATURE and fit is not None and day in temperatures:
            values = fit.predict(day, temperatures[day])
        elif kind == SEASONAL:
            values = predict(SEASONAL, days, day, None)
        if values is None:
            # No temperature for the day, or no model yet: the classic profile.
            values = predict(RECENT, days, day, None)
        if values is None:
            continue
        for hour, kwh in enumerate(values):
            hours[ConsumptionProfile.hour_key(day, hour)] = round(kwh, 4)
    return hours
