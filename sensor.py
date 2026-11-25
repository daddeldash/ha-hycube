"""Sensors for HyCube: live values, energy counters, forecast, schedule."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import (
    BATTERY_REST_MARGIN,
    CONF_EFFICIENCY,
    CONF_BATTERY_CAPACITY,
    DEFAULT_BATTERY_CAPACITY,
    ENERGY_BATTERY_CHARGE,
    ENERGY_BATTERY_DISCHARGE,
    ENERGY_EXTERNAL,
    ENERGY_GRID_EXPORT,
    ENERGY_GRID_IMPORT,
    ENERGY_HOME,
    ENERGY_SOLAR,
    MODE_GRID_CHARGE,
    MODES,
    SOURCES,
)
from .coordinator import HyCubeConfigEntry, HyCubeCoordinator
from .energy import to_float
from .entity import HyCubeEntity
from .model import KINDS

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class HyCubeValueDescription(SensorEntityDescription):
    """Sensor fed from /get_values/ (first key present wins)."""

    keys: tuple[str, ...]
    transform: Callable[[float], float] | None = None


def _power(key: str, *src: str, enabled: bool = True, **kw: Any) -> HyCubeValueDescription:
    return HyCubeValueDescription(
        key=key,
        keys=src,
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_registry_enabled_default=enabled,
        **kw,
    )


def _volt(key: str, *src: str, enabled: bool = False) -> HyCubeValueDescription:
    return HyCubeValueDescription(
        key=key,
        keys=src,
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=enabled,
    )


def _amp(key: str, *src: str, enabled: bool = False) -> HyCubeValueDescription:
    return HyCubeValueDescription(
        key=key,
        keys=src,
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=enabled,
    )


def _pos(v: float) -> float:
    return v if v > 0 else 0.0


def _neg(v: float) -> float:
    return -v if v < 0 else 0.0


VALUE_SENSORS: tuple[HyCubeValueDescription, ...] = (
    HyCubeValueDescription(
        key="battery_soc",
        keys=("Battery_C",),
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    _power("battery_power", "Battery_P"),
    _power("battery_charge_power", "Battery_P", transform=_pos),
    _power("battery_discharge_power", "Battery_P", transform=_neg),
    _volt("battery_voltage", "Battery_V", enabled=True),
    _amp("battery_current", "Battery_I", enabled=True),
    _power("grid_power", "Grid_P"),
    _power("grid_import_power", "Grid_P", transform=_pos),
    _power("grid_export_power", "Grid_P", transform=_neg),
    _power("grid_power_l1", "Grid_P_L1", enabled=False),
    _power("grid_power_l2", "Grid_P_L2", enabled=False),
    _power("grid_power_l3", "Grid_P_L3", enabled=False),
    _volt("grid_voltage", "Grid_V", enabled=True),
    _volt("grid_voltage_l1", "Grid_V_L1"),
    _volt("grid_voltage_l2", "Grid_V_L2"),
    _volt("grid_voltage_l3", "Grid_V_L3"),
    _amp("grid_current_l1", "Grid_I_L1"),
    _amp("grid_current_l2", "Grid_I_L2"),
    _amp("grid_current_l3", "Grid_I_L3"),
    HyCubeValueDescription(
        key="grid_frequency",
        keys=("Grid_f",),
        device_class=SensorDeviceClass.FREQUENCY,
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    _power("home_power", "Home_P"),
    _power("solar_power", "solar_total_P"),
    _power("solar1_power", "solar1_p", "solar1_P", "Solar1_P"),
    _power("solar2_power", "solar2_p", "solar2_P", "Solar2_P"),
    _volt("solar1_voltage", "solar1_v", "solar1_V", "Solar1_V"),
    _volt("solar2_voltage", "solar2_v", "solar2_V", "Solar2_V"),
    _amp("solar1_current", "solar1_I", "solar1_i", "Solar1_I"),
    _amp("solar2_current", "solar2_I", "solar2_i", "Solar2_I"),
    _power("inverter_power", "Inv1_P", enabled=False),
    _volt("inverter_voltage", "Inv1_V"),
    _amp("inverter_current", "Inv1_I"),
    _power("external_power", "Meter2_P", enabled=False),
)

ENERGY_SENSORS = (
    ENERGY_GRID_IMPORT,
    ENERGY_GRID_EXPORT,
    ENERGY_BATTERY_CHARGE,
    ENERGY_BATTERY_DISCHARGE,
    ENERGY_SOLAR,
    ENERGY_EXTERNAL,
    ENERGY_HOME,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HyCubeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        HyCubeValueSensor(coordinator, desc) for desc in VALUE_SENSORS
    ]
    entities += [HyCubeEnergySensor(coordinator, key) for key in ENERGY_SENSORS]
    entities += [
        HyCubeStoredEnergySensor(coordinator),
        HyCubeCapacityEstimateSensor(coordinator),
        HyCubeBatteryEmptySensor(coordinator),
        HyCubeForecastSensor(coordinator, "forecast_next_hour", _next_hour),
        HyCubeForecastSensor(coordinator, "forecast_rest_of_today", _rest_of_today),
        HyCubeForecastSensor(coordinator, "forecast_next_24h", _next_24h),
        HyCubeForecastSensor(coordinator, "forecast_tomorrow", _tomorrow),
        HyCubeBackfillSensor(coordinator),
        HyCubePlanSourceSensor(coordinator),
        HyCubePlanModeSensor(coordinator),
        HyCubeNextGridChargeSensor(coordinator),
        HyCubePlanSavingsSensor(coordinator),
        HyCubeEfficiencySensor(coordinator),
        HyCubeConsumptionModelSensor(coordinator),
        HyCubeForecastErrorSensor(coordinator),
        HyCubeModelAccuracySensor(coordinator),
        HyCubePvCorrectionSensor(coordinator),
        HyCubeRealizedSavingsSensor(coordinator),
    ]
    async_add_entities(entities)


class HyCubeValueSensor(HyCubeEntity, SensorEntity):
    """Live value from /get_values/."""

    entity_description: HyCubeValueDescription

    def __init__(self, coordinator: HyCubeCoordinator, desc: HyCubeValueDescription) -> None:
        super().__init__(coordinator, desc.key)
        self.entity_description = desc

    @property
    def native_value(self) -> float | None:
        values = self.coordinator.data.values if self.coordinator.data else {}
        for key in self.entity_description.keys:
            if (val := to_float(values.get(key))) is not None:
                if self.entity_description.transform:
                    val = self.entity_description.transform(val)
                # The device sends 32-bit floats (526.000022888184); without
                # rounding that noise is written as a new state every poll.
                precision = self.entity_description.suggested_display_precision
                return val if precision is None else round(val, precision + 1)
        return None


class HyCubeEnergySensor(HyCubeEntity, SensorEntity):
    """Energy counter integrated from power samples (for the Energy dashboard)."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: HyCubeCoordinator, key: str) -> None:
        super().__init__(coordinator, f"energy_{key}")
        self._key = key
        if key == ENERGY_EXTERNAL:
            self._attr_entity_registry_enabled_default = False

    @property
    def available(self) -> bool:
        # The counter is held by Home Assistant, so it stays valid while the
        # device is offline; missed energy is added after the backfill.
        return True

    @property
    def native_value(self) -> float:
        return round(self.coordinator.energy.totals[self._key] / 1000, 4)


def _capacity(coordinator: HyCubeCoordinator) -> float:
    return float(
        coordinator.config_entry.options.get(CONF_BATTERY_CAPACITY, DEFAULT_BATTERY_CAPACITY)
    )


def _soc(coordinator: HyCubeCoordinator) -> float | None:
    data = coordinator.data
    return to_float(data.values.get("Battery_C")) if data else None


class HyCubeStoredEnergySensor(HyCubeEntity, SensorEntity):
    """Energy currently stored (SoC x configured usable capacity)."""

    _attr_device_class = SensorDeviceClass.ENERGY_STORAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: HyCubeCoordinator) -> None:
        super().__init__(coordinator, "battery_stored_energy")

    @property
    def native_value(self) -> float | None:
        if (soc := _soc(self.coordinator)) is None:
            return None
        return round(soc / 100 * _capacity(self.coordinator), 3)


class HyCubeCapacityEstimateSensor(HyCubeEntity, SensorEntity):
    """Diagnostics: energy the battery delivered per 100 % SoC while discharging."""

    _attr_device_class = SensorDeviceClass.ENERGY_STORAGE
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 1
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: HyCubeCoordinator) -> None:
        super().__init__(coordinator, "battery_capacity_estimate")

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> float | None:
        return self.coordinator.capacity.kwh

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        est = self.coordinator.capacity
        return {
            "samples": est.samples,
            "last_sample_kwh": est.last_sample,
            "configured_kwh": _capacity(self.coordinator),
        }


class HyCubeBatteryEmptySensor(HyCubeEntity, SensorEntity):
    """When the battery reaches its reserve at forecast consumption, without PV.

    The value is a timestamp and every change shows up in the logbook, so it
    is kept still: an empty battery reports since when it is empty, and the
    estimate moves in steps of a quarter of an hour.
    """

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _STEP = timedelta(minutes=15)

    def __init__(self, coordinator: HyCubeCoordinator) -> None:
        super().__init__(coordinator, "battery_empty_estimate")
        self._empty_since: datetime | None = None
        self._reported: datetime | None = None

    def _usable(self) -> float | None:
        """kWh the battery still delivers.

        It stops at the reserve and its reading then recovers a few percent,
        so a state of charge just above the reserve counts as empty.
        """
        if (soc := _soc(self.coordinator)) is None:
            return None
        floor = self.coordinator.discharge_floor()
        if soc <= floor + BATTERY_REST_MARGIN:
            return 0.0
        return (soc - floor) / 100 * _capacity(self.coordinator)

    def _estimate(self, remaining: float) -> datetime | None:
        profile = self.coordinator.profile
        cursor = dt_util.now()
        for _ in range(48):
            hour_end = cursor.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            need = profile.forecast(cursor, hour_end) or 0.0
            if need >= remaining:
                frac = remaining / need if need else 0
                return cursor + (hour_end - cursor) * frac
            remaining -= need
            cursor = hour_end
        return None  # lasts more than 48 h

    @property
    def native_value(self) -> datetime | None:
        if (remaining := self._usable()) is None or not self.coordinator.profile.ready:
            return None
        if remaining <= 0:
            if self._empty_since is None:
                self._empty_since = dt_util.now().replace(second=0, microsecond=0)
            self._reported = None
            return self._empty_since
        self._empty_since = None
        if (estimate := self._estimate(remaining)) is None:
            self._reported = None
            return None
        # Only move once the estimate is a full step away from what is shown.
        if self._reported is None or abs(estimate - self._reported) >= self._STEP:
            self._reported = estimate.replace(
                minute=estimate.minute // 15 * 15, second=0, microsecond=0
            )
        return self._reported

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        usable = self._usable()
        floor = self.coordinator.discharge_floor()
        return {
            "empty": None if usable is None else usable <= 0,
            "reserve_soc": floor,
            "empty_below_soc": floor + BATTERY_REST_MARGIN,
            "usable_energy_kwh": None if usable is None else round(usable, 2),
        }


def _next_hour(c: HyCubeCoordinator, now: datetime) -> float | None:
    return c.profile.forecast(now, now + timedelta(hours=1))


def _rest_of_today(c: HyCubeCoordinator, now: datetime) -> float | None:
    return c.profile.forecast(now, dt_util.start_of_local_day(now) + timedelta(days=1))


def _next_24h(c: HyCubeCoordinator, now: datetime) -> float | None:
    return c.profile.forecast(now, now + timedelta(hours=24))


def _tomorrow(c: HyCubeCoordinator, now: datetime) -> float | None:
    start = dt_util.start_of_local_day(now) + timedelta(days=1)
    return c.profile.forecast(start, start + timedelta(days=1))


class HyCubeForecastSensor(HyCubeEntity, SensorEntity):
    """Consumption forecast from the weekday/hour profile."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 2
    # No state_class: a forecast must not create long-term statistics.
    _unrecorded_attributes = frozenset({"hourly", "profile_today"})

    def __init__(
        self,
        coordinator: HyCubeCoordinator,
        key: str,
        func: Callable[[HyCubeCoordinator, datetime], float | None],
    ) -> None:
        super().__init__(coordinator, key)
        self._func = func
        self._key = key

    @property
    def available(self) -> bool:
        return self.coordinator.profile.ready

    @property
    def native_value(self) -> float | None:
        val = self._func(self.coordinator, dt_util.now())
        # 10 Wh resolution keeps the recorder from writing a row every poll.
        return None if val is None else round(val, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self._key != "forecast_next_24h":
            return None
        profile = self.coordinator.profile
        now = dt_util.now()
        today = dt_util.start_of_local_day(now)
        return {
            "hourly": profile.hourly_forecast(now, 24),
            "profile_today": [
                round(profile.expected(today + timedelta(hours=h)) or 0.0, 3)
                for h in range(24)
            ],
            "model": self.coordinator.forecaster.kind,
            "profile_days_of_data": round(profile.days_of_data, 1),
            "profile_built": profile.built.isoformat() if profile.built else None,
            "profile_source": self.coordinator.profile_source(),
        }


class HyCubeBackfillSensor(HyCubeEntity, SensorEntity):
    """Diagnostics: last time a connection gap was filled."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: HyCubeCoordinator) -> None:
        super().__init__(coordinator, "last_backfill")

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> datetime | None:
        ts = self.coordinator.last_backfill.get("time")
        return dt_util.parse_datetime(ts) if ts else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = {k: v for k, v in self.coordinator.last_backfill.items() if k != "time"}
        return attrs


def _iso(timestamp: float) -> str:
    return dt_util.as_local(dt_util.utc_from_timestamp(timestamp)).isoformat()


class HyCubePlanSourceSensor(HyCubeEntity, SensorEntity):
    """Schedule: what supplies the home right now, with the whole plan as attributes."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = SOURCES
    _unrecorded_attributes = frozenset({"schedule", "slots"})

    def __init__(self, coordinator: HyCubeCoordinator) -> None:
        super().__init__(coordinator, "plan_source")
        self._attrs_for: datetime | None = None
        self._plan_attrs: dict[str, Any] = {}

    @property
    def available(self) -> bool:
        # Stays available without a plan so the reason is visible.
        return True

    @property
    def native_value(self) -> str | None:
        slot = self.coordinator.planner.current_slot()
        return slot.source if slot else None

    def _plan_attributes(self) -> dict[str, Any]:
        """Attributes that only change with a new plan, built once per plan."""
        planner = self.coordinator.planner
        if planner.plan is None or planner.battery is None:
            return {}
        if self._attrs_for == planner.calculated:
            return self._plan_attrs
        plan, battery = planner.plan, planner.battery
        last = plan.slots[-1] if plan.slots else None
        self._plan_attrs = {
            "horizon_end": _iso(last.start + last.hours * 3600) if last else None,
            "capacity_kwh": round(battery.capacity, 2),
            "efficiency": round(battery.charge_eff * 100, 1),
            "reserve_soc": battery.floor,
            "schedule": [
                {
                    "start": _iso(b.start),
                    "end": _iso(b.end),
                    "source": b.source,
                    "mode": b.mode,
                    "price": round(b.price, 4),
                    "load_kwh": round(b.load, 2),
                    "pv_kwh": round(b.pv, 2),
                    "grid_kwh": round(b.grid, 2),
                    "battery_kwh": round(b.battery, 2),
                    "soc": round(b.soc),
                }
                for b in plan.blocks()
            ],
            "slots": [
                {
                    "start": _iso(s.start),
                    "source": s.source,
                    "mode": s.mode,
                    "price": round(s.price, 4),
                    "load_kwh": round(s.load, 3),
                    "pv_kwh": round(s.pv, 3),
                    "grid_kwh": round(s.grid, 3),
                    "battery_kwh": round(s.battery, 3),
                    "soc": round(s.soc, 1),
                }
                for s in plan.slots
            ],
        }
        self._attrs_for = planner.calculated
        return self._plan_attrs

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        planner = self.coordinator.planner
        slot = planner.current_slot()
        return {
            "mode": slot.mode if slot else None,
            "error": planner.error,
            "calculated": planner.calculated.isoformat() if planner.calculated else None,
            "tibber_home": planner.home,
            "pv_forecast": planner.pv_forecast,
            "hold_blocked": planner.hold_blocked,
            "paused": planner.paused,
            "automatic": planner.auto,
            **self._plan_attributes(),
        }


class HyCubePlanEntity(HyCubeEntity, SensorEntity):
    """Sensor that needs a schedule."""

    @property
    def available(self) -> bool:
        return self.coordinator.planner.plan is not None


class HyCubePlanModeSensor(HyCubePlanEntity):
    """Schedule: battery mode the plan asks for right now."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = MODES

    def __init__(self, coordinator: HyCubeCoordinator) -> None:
        super().__init__(coordinator, "plan_mode")

    @property
    def native_value(self) -> str:
        return self.coordinator.planner.wanted()[0]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        mode, target = self.coordinator.planner.wanted()
        return {"target_soc": target if mode == MODE_GRID_CHARGE else None}


class HyCubeNextGridChargeSensor(HyCubePlanEntity):
    """Schedule: start of the next stretch of charging from the grid."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: HyCubeCoordinator) -> None:
        super().__init__(coordinator, "plan_next_grid_charge")

    @property
    def native_value(self) -> datetime | None:
        plan = self.coordinator.planner.plan
        block = plan.next_grid_charge() if plan else None
        return dt_util.utc_from_timestamp(block.start) if block else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        plan = self.coordinator.planner.plan
        block = plan.next_grid_charge() if plan else None
        coordinator = self.coordinator
        power = {
            "charge_power_w": round(coordinator.planner.grid_charge_power()),
            "peak_charge_w": _rounded(coordinator.peak_charge),
            "peak_discharge_w": _rounded(coordinator.peak_discharge),
        }
        if block is None:
            return {"end": None, "target_soc": None, "energy_kwh": None, "price": None, **power}
        return {
            "end": _iso(block.end),
            "target_soc": round(block.soc),
            "energy_kwh": round(block.battery, 2),
            "price": round(block.price, 4),
            **power,
        }


def _rounded(value: float | None) -> int | None:
    return None if value is None else round(value)


class HyCubePlanSavingsSensor(HyCubePlanEntity):
    """Schedule: saving against standard mode, with the cautious assumptions
    the plan was decided on; `expected` is the figure if the forecast holds."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: HyCubeCoordinator) -> None:
        super().__init__(coordinator, "plan_savings")
        self._attr_native_unit_of_measurement = coordinator.hass.config.currency

    @property
    def native_value(self) -> float | None:
        plan = self.coordinator.planner.plan
        return round(plan.savings, 2) if plan else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        plan = self.coordinator.planner.plan
        if plan is None:
            return None
        return {
            "expected": None if plan.expected is None else round(plan.expected, 2),
            "cost": round(plan.cost, 2),
            "cost_standard": round(plan.baseline_cost, 2),
            "hours": round(sum(s.hours for s in plan.slots), 2),
        }


class HyCubeEfficiencySensor(HyCubeEntity, SensorEntity):
    """Diagnostics: share of the charged energy the battery gives back."""

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = 0
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: HyCubeCoordinator) -> None:
        super().__init__(coordinator, "efficiency_estimate")

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> float | None:
        efficiency = self.coordinator.efficiency()
        return None if efficiency is None else round(efficiency * 100, 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        coordinator = self.coordinator
        est = coordinator.charge_capacity
        measured = coordinator.counter_efficiency
        planner = coordinator.planner
        used = planner.battery_model().charge_eff
        return {
            "source": "counters" if measured else ("capacity" if est.samples else None),
            "window_start": measured.start.isoformat() if measured else None,
            "window_end": measured.end.isoformat() if measured else None,
            "charged_kwh": round(measured.charged, 1) if measured else None,
            "discharged_kwh": round(measured.discharged, 1) if measured else None,
            "charge_kwh_per_100": est.kwh,
            "samples": est.samples,
            "used_for_schedule": round(used * 100, 1),
            "decided_with": round(used * planner.efficiency_caution() * 100, 1),
            "set_in_options": bool(coordinator.config_entry.options.get(CONF_EFFICIENCY)),
        }


def _percent(share: float | None) -> float | None:
    return None if share is None else round(share * 100, 1)


class HyCubeConsumptionModelSensor(HyCubeEntity, SensorEntity):
    """Diagnostics: kind of model the consumption forecast uses, and why."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = KINDS
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: HyCubeCoordinator) -> None:
        super().__init__(coordinator, "consumption_model")

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> str:
        return self.coordinator.forecaster.kind

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        forecaster = self.coordinator.forecaster
        archive = self.coordinator.archive
        test, fit = forecaster.backtest, forecaster.fit
        oldest = archive.oldest()
        return {
            "backtest_hourly_error": {k: _percent(v) for k, v in test.hourly.items()} if test else {},
            "backtest_daily_error": {k: _percent(v) for k, v in test.daily.items()} if test else {},
            "backtest_days": test.days if test else 0,
            "archive_days": archive.count(),
            "archive_oldest": oldest.isoformat() if oldest else None,
            "archive_complete": archive.complete,
            "base_load_kwh": round(fit.daily_base, 1) if fit else None,
            "kwh_per_kelvin": round(fit.daily_slope, 2) if fit else None,
            "base_temperature": fit.base if fit else None,
            "weather_offset": None if forecaster.offset is None else round(forecaster.offset, 1),
            "temperatures": {day.isoformat(): t for day, t in forecaster.temperatures.items()},
        }


def _accuracy(share: float | None) -> float | None:
    """100 % minus the error, never below 0."""
    return None if share is None else round(max(1 - share, 0.0) * 100, 1)


class HyCubeModelAccuracySensor(HyCubeEntity, SensorEntity):
    """Diagnostics: how well the consumption model in use hits the daily consumption.

    From the nightly backtest: the model predicted each of the last days
    from the days before it. 100 % minus the share the daily totals were off.
    """

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: HyCubeCoordinator) -> None:
        super().__init__(coordinator, "consumption_model_accuracy")

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> float | None:
        forecaster = self.coordinator.forecaster
        test = forecaster.backtest
        return _accuracy(test.daily.get(forecaster.kind)) if test else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        forecaster = self.coordinator.forecaster
        test, error = forecaster.backtest, forecaster.error
        return {
            "model": forecaster.kind,
            "hourly_accuracy": _accuracy(test.hourly.get(forecaster.kind)) if test else None,
            "days": test.days if test else 0,
            "all_models": {k: _accuracy(v) for k, v in test.daily.items()} if test else {},
            # Of the forecasts really made, once there are some.
            "measured_accuracy": _accuracy(error[0]) if error else None,
            "measured_days": error[2] if error else 0,
        }


class HyCubeForecastErrorSensor(HyCubeEntity, SensorEntity):
    """Diagnostics: how far the consumption forecast was off over the last days."""

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: HyCubeCoordinator) -> None:
        super().__init__(coordinator, "consumption_forecast_error")

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> float | None:
        error = self.coordinator.forecaster.error
        return _percent(error[0]) if error else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        error = self.coordinator.forecaster.error
        return {
            "hourly_error": _percent(error[1]) if error else None,
            "days": error[2] if error else 0,
        }


class HyCubePvCorrectionSensor(HyCubeEntity, SensorEntity):
    """Diagnostics: learned factor between real and forecast PV production."""

    _attr_suggested_display_precision = 2
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: HyCubeCoordinator) -> None:
        super().__init__(coordinator, "pv_forecast_correction")

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> float | None:
        correction = self.coordinator.forecaster.pv
        return correction.overall if correction else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        correction = self.coordinator.forecaster.pv
        return {
            "hourly": list(correction.hourly) if correction else None,
            "days": correction.days if correction else 0,
            "used_for_schedule": self.coordinator.planner.pv_correction() is not None,
        }


class HyCubeRealizedSavingsSensor(HyCubeEntity, SensorEntity):
    """What the schedule really saved over the last two weeks."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: HyCubeCoordinator) -> None:
        super().__init__(coordinator, "plan_realized_savings")
        self._attr_native_unit_of_measurement = coordinator.hass.config.currency

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> float | None:
        audit = self.coordinator.forecaster.audit
        return audit.total if audit and audit.active_days else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        audit = self.coordinator.forecaster.audit
        return {
            "active_days": audit.active_days if audit else 0,
            "model_error_per_day": audit.bias if audit else None,
            "days": audit.days if audit else {},
            "paused": self.coordinator.planner.paused,
        }
