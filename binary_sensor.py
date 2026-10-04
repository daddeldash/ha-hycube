"""Binary sensors from the /data_row/ status flags."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import HyCubeConfigEntry, HyCubeCoordinator
from .energy import to_float
from .entity import HyCubeEntity

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class HyCubeFlagDescription(BinarySensorEntityDescription):
    """Flag in /data_row/."""

    flag: str


FLAGS: tuple[HyCubeFlagDescription, ...] = (
    HyCubeFlagDescription(
        key="grid_charging_active",
        flag="manualChargingActivation",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
    ),
    HyCubeFlagDescription(
        key="battery_schedule_active",
        flag="bat_schedule_Active",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    HyCubeFlagDescription(
        key="eps_enabled",
        flag="EPS_enable_r",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    HyCubeFlagDescription(
        key="eps_overload",
        flag="EPS_overloadState",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HyCubeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up binary sensors."""
    async_add_entities(HyCubeFlag(entry.runtime_data, desc) for desc in FLAGS)


class HyCubeFlag(HyCubeEntity, BinarySensorEntity):
    """Status flag."""

    entity_description: HyCubeFlagDescription

    def __init__(self, coordinator: HyCubeCoordinator, desc: HyCubeFlagDescription) -> None:
        super().__init__(coordinator, desc.key)
        self.entity_description = desc

    @property
    def is_on(self) -> bool | None:
        status = self.coordinator.data.status if self.coordinator.data else {}
        val = to_float(status.get(self.entity_description.flag))
        return None if val is None else val != 0
