"""HyCube battery storage (local CubeConnect API)."""

from __future__ import annotations

import logging

from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import HyCubeApi
from .const import (
    CONF_CMD_GRID_CHARGE,
    CONF_CMD_STANDARD,
    LEGACY_CMD_GRID_CHARGE,
    LEGACY_CMD_STANDARD,
)
from .coordinator import HyCubeConfigEntry, HyCubeCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
]


async def async_setup_entry(hass: HomeAssistant, entry: HyCubeConfigEntry) -> bool:
    """Set up HyCube from a config entry."""
    api = HyCubeApi(
        async_get_clientsession(hass),
        entry.data[CONF_HOST],
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
    )
    coordinator = HyCubeCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.start_profile_schedule()
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: HyCubeConfigEntry) -> bool:
    """1.1 -> 1.2: modes send several requests (battery split for "hold")."""
    if entry.version > 1:
        return False
    if entry.minor_version < 2:
        options = dict(entry.options)
        # Only replace commands still at the old single-request default, so
        # "standard" also restores the battery split after "hold".
        for key, legacy in (
            (CONF_CMD_STANDARD, LEGACY_CMD_STANDARD),
            (CONF_CMD_GRID_CHARGE, LEGACY_CMD_GRID_CHARGE),
        ):
            if options.get(key, legacy).strip() == legacy:
                options.pop(key, None)  # fall back to the new default
        hass.config_entries.async_update_entry(entry, options=options, minor_version=2)
        _LOGGER.info("Migrated HyCube entry to 1.2")
    return True


async def _async_options_updated(hass: HomeAssistant, entry: HyCubeConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: HyCubeConfigEntry) -> bool:
    """Unload a config entry."""
    if unloaded := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        # Flush the energy totals now; a pending delayed save of this
        # coordinator must not overwrite the store after a reload.
        await entry.runtime_data.async_shutdown()
    return unloaded
