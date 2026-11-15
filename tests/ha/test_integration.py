"""Set the integration up with a simulated device and check the schedule."""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from common import CHARGE, DOMAIN, SERIAL, VALUES, setup, state


async def test_schedule_sensors(hass: HomeAssistant, api, tibber) -> None:
    entry = await setup(hass)
    planner = entry.runtime_data.planner
    assert planner.error is None
    assert planner.home == "Home"

    source = state(hass, "sensor", "plan_source")
    assert source.state == "grid_charge"  # cheap now, expensive later
    assert source.attributes["mode"] == "grid_charge"
    assert source.attributes["error"] is None
    assert source.attributes["pv_forecast"] is False
    blocks = source.attributes["schedule"]
    assert blocks[0]["source"] == "grid_charge"
    assert {b["source"] for b in blocks} >= {"grid_charge", "battery"}
    assert len(source.attributes["slots"]) > 96

    assert state(hass, "sensor", "plan_mode").state == "grid_charge"
    assert state(hass, "sensor", "plan_next_grid_charge").attributes["target_soc"] > 40
    assert float(state(hass, "sensor", "plan_savings").state) > 0
    assert state(hass, "sensor", "efficiency_estimate").attributes["used_for_schedule"] == 80
    # Float noise of the device is rounded away.
    assert state(hass, "sensor", "grid_power").state == "500.0"

    # Off by default: nothing is sent to the device.
    assert state(hass, "switch", "plan_auto").state == "off"
    api.send_command.assert_not_called()


async def test_automatic_mode_follows_the_plan(hass: HomeAssistant, api, tibber) -> None:
    entry = await setup(hass)
    coordinator = entry.runtime_data
    switch = er.async_get(hass).async_get_entity_id("switch", DOMAIN, f"{SERIAL}_plan_auto")

    await hass.services.async_call("switch", "turn_on", {"entity_id": switch}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert coordinator.mode == "grid_charge"
    sent = [call.args[0] for call in api.send_command.call_args_list]
    assert sent[-1] == f"{CHARGE}{coordinator.target_soc}"
    assert coordinator.target_soc > 40
    assert state(hass, "binary_sensor", "grid_charging_active").state == "on"

    api.send_command.reset_mock()
    await hass.services.async_call("switch", "turn_off", {"entity_id": switch}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert coordinator.mode == "standard"
    assert api.send_command.call_args_list[0].args[0] == CHARGE


async def test_no_prices_means_no_plan(hass: HomeAssistant, api) -> None:
    entry = await setup(hass)
    assert entry.runtime_data.planner.error == "no_prices"
    source = state(hass, "sensor", "plan_source")
    assert source.state == "unknown"
    assert source.attributes["error"] == "no_prices"
    assert state(hass, "sensor", "plan_mode").state == "unavailable"


async def test_pv_forecast_of_the_energy_dashboard(hass: HomeAssistant, api, tibber) -> None:
    assert await async_setup_component(hass, "energy", {})
    forecast_entry = MockConfigEntry(domain="fake_forecast")
    forecast_entry.add_to_hass(hass)
    from homeassistant.components.energy.data import async_get_manager

    manager = await async_get_manager(hass)
    await manager.async_update(
        {
            "energy_sources": [
                {
                    "type": "solar",
                    "stat_energy_from": "sensor.pv_energy",
                    "config_entry_solar_forecast": [forecast_entry.entry_id],
                }
            ]
        }
    )
    hour = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)

    async def forecast(hass: HomeAssistant, entry_id: str) -> dict:
        assert entry_id == forecast_entry.entry_id
        return {"wh_hours": {(hour + timedelta(hours=h)).isoformat(): 4000 for h in range(1, 4)}}

    with patch(
        "homeassistant.components.energy.websocket_api.async_get_energy_platforms",
        AsyncMock(return_value={"fake_forecast": forecast}),
    ):
        entry = await setup(hass, {"tibber_home": "Home", "pv_factor": 0.5})

    source = state(hass, "sensor", "plan_source")
    assert source.attributes["pv_forecast"] is True
    slots = entry.runtime_data.planner.plan.slots
    # 3 h x 4 kWh, halved by the correction factor.
    assert sum(s.pv for s in slots) == pytest.approx(6.0)
    assert "solar" in {s.source for s in slots}


async def test_options_menu_keeps_the_other_step(hass: HomeAssistant, api, tibber) -> None:
    entry = await setup(hass, {"tibber_home": "Home", "battery_capacity": 6.0})

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "schedule"}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "plan_grid_charge": True,
            "plan_hold": False,
            "grid_charge_power": 3000,
            "max_charge_soc": 90,
            "shift_cost": 2.0,
            "export_price": 8.0,
            "pv_factor": 0.9,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.options["battery_capacity"] == 6.0  # other step untouched
    assert "tibber_home" not in entry.options  # left empty: removed
    assert entry.options["plan_hold"] is False

    model = entry.runtime_data.planner.battery_model()
    assert model.allow_hold is False
    assert model.grid_power == 3.0
    assert model.max_grid_soc == 90
    assert model.shift_cost == pytest.approx(0.02)
    assert model.export_price == pytest.approx(0.08)
    # Two homes and none chosen: the first one by name is used.
    assert entry.runtime_data.planner.home == "Home"


async def test_hold_is_dropped_when_the_device_recharges_from_the_grid(
    hass: HomeAssistant, api, tibber
) -> None:
    entry = await setup(hass)
    coordinator = entry.runtime_data
    planner = coordinator.planner
    assert planner.battery_model().allow_hold is True

    await coordinator.async_set_mode("hold")
    assert api.send_command.call_args_list[-1].args[0] == (
        "/Bat/setCustomBat/?x_active=60&x_passive=5"
    )
    # The battery charges without PV: the reserve is refilled from the grid.
    api.get_values.return_value = dict(VALUES, Battery_P="400")
    for _ in range(3):
        await coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert planner.hold_blocked is True
    assert planner.battery_model().allow_hold is False
    assert state(hass, "sensor", "plan_source").attributes["hold_blocked"] is True

    # Leaving "hold": the plan expects the battery to stay idle for 1.5 h.
    api.get_values.return_value = VALUES
    await coordinator.async_set_mode("standard")
    assert 5300 < coordinator.release_idle_seconds() <= 5400
    # Seen discharging again: no more waiting.
    api.get_values.return_value = dict(VALUES, Battery_P="-300")
    await coordinator.async_refresh()
    assert coordinator.release_idle_seconds() == 0
