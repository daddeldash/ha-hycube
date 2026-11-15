"""Switch that lets the integration carry out the schedule."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
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
    """Set up the switch."""
    async_add_entities([HyCubeAutoPlanSwitch(entry.runtime_data)])


class HyCubeAutoPlanSwitch(HyCubeEntity, SwitchEntity):
    """On: the battery mode follows the schedule."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: HyCubeCoordinator) -> None:
        super().__init__(coordinator, "plan_auto")

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.planner.auto

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.planner.async_set_auto(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.planner.async_set_auto(False)
