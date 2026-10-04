"""Diagnostics download."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .coordinator import HyCubeConfigEntry

TO_REDACT = {CONF_PASSWORD, CONF_USERNAME, "HYCUBE_SERIAL", "HYCUBE SERIAL"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: HyCubeConfigEntry
) -> dict[str, Any]:
    """Raw device data, energy state and profile."""
    c = entry.runtime_data
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "info": async_redact_data(c.info, TO_REDACT),
        "values": c.data.values if c.data else None,
        "status": c.data.status if c.data else None,
        "energy_totals_wh": c.energy.totals,
        "last_sample": c.energy.last,
        "last_backfill": c.last_backfill,
        "history_days": len([v for v in c.history.values() if v]),
        "mode": c.mode,
        "target_soc": c.target_soc,
        "profile": c.profile.as_dict(),
    }
