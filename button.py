"""Button to rebuild the consumption profile on demand."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
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
    """Set up buttons."""
    async_add_entities([HyCubeRebuildProfileButton(entry.runtime_data)])


class HyCubeRebuildProfileButton(HyCubeEntity, ButtonEntity):
    """Recompute the profile from recorder statistics."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: HyCubeCoordinator) -> None:
        super().__init__(coordinator, "rebuild_profile")

    @property
    def available(self) -> bool:
        return True

    async def async_press(self) -> None:
        await self.coordinator.async_rebuild_profile()
