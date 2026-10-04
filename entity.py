"""Base entity for HyCube."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import HyCubeCoordinator


class HyCubeEntity(CoordinatorEntity[HyCubeCoordinator]):
    """Common device info and unique id handling."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: HyCubeCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_translation_key = key
        self._attr_unique_id = f"{coordinator.config_entry.unique_id}_{key}"
        info = coordinator.info
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.config_entry.unique_id)},
            manufacturer=MANUFACTURER,
            model=info.get("HYCUBE_TYPE") or info.get("HYCUBE MACHINE") or "HyCube",
            serial_number=info.get("HYCUBE_SERIAL"),
            sw_version=str(info.get("HyWeb_Version") or info.get("HyWeb Version") or ""),
            hw_version=info.get("HYCUBE_CONTROLLER"),
            name="HyCube",
            configuration_url=coordinator.api.host,
        )
