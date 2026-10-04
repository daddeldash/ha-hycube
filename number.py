"""Target state of charge for grid charging."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import HyCubeConfigEntry, HyCubeCoordinator
from .entity import HyCubeEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HyCubeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the target SoC number."""
    async_add_entities([HyCubeTargetSoc(entry.runtime_data)])


class HyCubeTargetSoc(HyCubeEntity, NumberEntity):
    """Grid charging stops at this SoC."""

    _attr_native_min_value = 10
    _attr_native_max_value = 100
    _attr_native_step = 5
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: HyCubeCoordinator) -> None:
        super().__init__(coordinator, "grid_charge_target_soc")

    @property
    def native_value(self) -> float:
        return self.coordinator.target_soc

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_target_soc(int(value))
