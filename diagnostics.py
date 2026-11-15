"""Diagnostics download."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .const import CONF_TIBBER_HOME
from .coordinator import HyCubeConfigEntry
from .forecaster import ConsumptionForecaster
from .planner import HyCubePlanner

# The Tibber home is named after its address.
TO_REDACT = {
    CONF_PASSWORD,
    CONF_USERNAME,
    CONF_TIBBER_HOME,
    "HYCUBE_SERIAL",
    "HYCUBE SERIAL",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: HyCubeConfigEntry
) -> dict[str, Any]:
    """Raw device data, energy state and profile."""
    c = entry.runtime_data
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": async_redact_data(dict(entry.options), TO_REDACT),
        "info": async_redact_data(c.info, TO_REDACT),
        "values": c.data.values if c.data else None,
        "status": c.data.status if c.data else None,
        "energy_totals_wh": c.energy.totals,
        "last_sample": c.energy.last,
        "last_backfill": c.last_backfill,
        "capacity_estimate": c.capacity.as_dict(),
        "discharge_floor": c.discharge_floor(),
        "archive": {
            "days": c.archive.count(),
            "oldest": c.archive.oldest(),
            "complete": c.archive.complete,
        },
        "consumption_model": _model(c.forecaster),
        "mode": c.mode,
        "target_soc": c.target_soc,
        "profile": c.profile.as_dict(),
        "charge_capacity_estimate": c.charge_capacity.as_dict(),
        "efficiency": c.efficiency(),
        "schedule": _schedule(c.planner),
    }


def _model(forecaster: ConsumptionForecaster) -> dict[str, Any]:
    return {
        "kind": forecaster.kind,
        "backtest": asdict(forecaster.backtest) if forecaster.backtest else None,
        "temperature_fit": asdict(forecaster.fit) if forecaster.fit else None,
        "weather_offset": forecaster.offset,
        "temperatures": {day.isoformat(): t for day, t in forecaster.temperatures.items()},
        "measured_error": forecaster.error,
        "pv_correction": asdict(forecaster.pv) if forecaster.pv else None,
        "audit": asdict(forecaster.audit) if forecaster.audit else None,
    }


def _schedule(planner: HyCubePlanner) -> dict[str, Any]:
    plan = planner.plan
    return {
        **planner.as_dict(),
        "error": planner.error,
        "calculated": planner.calculated,
        "prices_available": planner.home is not None,
        "pv_forecast": planner.pv_forecast,
        "battery_model": asdict(planner.battery_model()),
        "paused": planner.paused,
        "peak_charge_w": planner.coordinator.peak_charge,
        "peak_discharge_w": planner.coordinator.peak_discharge,
        "expected_savings": plan.expected if plan else None,
        "cost": plan.cost if plan else None,
        "cost_standard": plan.baseline_cost if plan else None,
        "savings": plan.savings if plan else None,
        "blocks": [asdict(block) for block in plan.blocks()] if plan else None,
    }
