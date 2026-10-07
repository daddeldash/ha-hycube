"""Sensors for HyCube: live values, energy counters, profile forecast."""

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
    CONF_BATTERY_CAPACITY,
    DEFAULT_BATTERY_CAPACITY,
    ENERGY_BATTERY_CHARGE,
    ENERGY_BATTERY_DISCHARGE,
    ENERGY_EXTERNAL,
    ENERGY_GRID_EXPORT,
    ENERGY_GRID_IMPORT,
    ENERGY_HOME,
    ENERGY_SOLAR,
)
from .coordinator import HyCubeConfigEntry, HyCubeCoordinator
from .energy import to_float
from .entity import HyCubeEntity

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
                return val
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
    """When the battery reaches its reserve at profile consumption, without PV."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: HyCubeCoordinator) -> None:
        super().__init__(coordinator, "battery_empty_estimate")

    def _usable(self) -> float | None:
        """kWh above the reserve limit."""
        if (soc := _soc(self.coordinator)) is None:
            return None
        floor = self.coordinator.discharge_floor()
        return max(soc - floor, 0.0) / 100 * _capacity(self.coordinator)

    @property
    def native_value(self) -> datetime | None:
        profile = self.coordinator.profile
        if (remaining := self._usable()) is None or not profile.ready:
            return None
        cursor = dt_util.now()
        for _ in range(48):
            hour_end = cursor.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            need = profile.forecast(cursor, hour_end) or 0.0
            if need >= remaining:
                frac = remaining / need if need else 0
                empty = cursor + (hour_end - cursor) * frac
                return empty.replace(second=0, microsecond=0)
            remaining -= need
            cursor = hour_end
        return None  # lasts more than 48 h

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        usable = self._usable()
        return {
            "reserve_soc": self.coordinator.discharge_floor(),
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
        return {
            "hourly": profile.hourly_forecast(now, 24),
            "profile_today": profile.day_profile(now.weekday()),
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
