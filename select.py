"""Battery operating mode."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import MODES
from .coordinator import HyCubeConfigEntry, HyCubeCoordinator
from .entity import HyCubeEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HyCubeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the mode select."""
    async_add_entities([HyCubeModeSelect(entry.runtime_data)])


class HyCubeModeSelect(HyCubeEntity, SelectEntity):
    """standard / hold / grid_charge."""

    _attr_options = MODES

    def __init__(self, coordinator: HyCubeCoordinator) -> None:
        super().__init__(coordinator, "battery_mode")

    @property
    def current_option(self) -> str:
        return self.coordinator.mode

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_mode(option)
