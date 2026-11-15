"""Set the integration up with a simulated device and check the schedule."""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

DOMAIN = "hycube"
SERIAL = "SN123"

VALUES = {
    "Battery_C": "40",
    "Battery_P": "0",
    "Battery_V": "51.2",
    "Battery_I": "0",
    "Grid_P": "500.000022888184",
    "Home_P": "500",
    "Inv1_P": "0",
    "solar_total_P": "0",
}
STATUS = {"manualChargingActivation": "0", "manualChargingSoc": "80"}
CHARGE = "/smartCharging/ManualChargingActivation/?value="
# One day of 5-minute statistics with a constant 600 W consumption.
DAY_STATS = {
    "Zeitstempel": [
        f"{(i * 5 // 60) % 24:02d}:{i * 5 % 60:02d}:00" for i in range(1, 289)
    ],
    "hausverbrauch": [600] * 288,
}


def prices(cheap_hours: float) -> dict:
    """Cheap for the next hours, expensive afterwards."""
    start = dt_util.start_of_local_day()
    switch = dt_util.now() + timedelta(hours=cheap_hours)
    rows = []
    for i in range(2 * 96):
        begin = start + timedelta(minutes=15 * i)
        rows.append({"start_time": begin.isoformat(), "price": 0.10 if begin < switch else 0.50})
    return {"prices": {"Home": rows, "Other": [dict(r, price=r["price"] + 1) for r in rows]}}


@pytest.fixture
def api():
    """The device."""
    with (
        patch("custom_components.hycube.HyCubeApi") as cls,
        patch("custom_components.hycube.coordinator.HISTORY_REQUEST_SPACING", 0),
    ):
        device = cls.return_value
        device.host = "http://192.0.2.1"
        device.get_info = AsyncMock(return_value={"HYCUBE_SERIAL": SERIAL})
        device.get_values = AsyncMock(return_value=VALUES)
        status = dict(STATUS)

        async def send_command(command: str) -> str:
            # The device reports grid charging the way it was last set.
            if command.startswith(CHARGE):
                target = command.removeprefix(CHARGE)
                status["manualChargingActivation"] = "1" if target else "0"
                if target:
                    status["manualChargingSoc"] = target
            return "ok"

        device.get_data_row = AsyncMock(side_effect=lambda: dict(status))
        device.get_day_stats = AsyncMock(return_value=DAY_STATS)
        device.send_command = AsyncMock(side_effect=send_command)
        yield device


@pytest.fixture
def tibber(hass: HomeAssistant):
    """The Tibber service; the returned dict is what it answers."""
    answer = prices(cheap_hours=3)

    async def get_prices(call: ServiceCall) -> dict:
        return answer

    hass.services.async_register(
        "tibber", "get_prices", get_prices, supports_response=SupportsResponse.ONLY
    )
    return answer


async def setup(hass: HomeAssistant, options: dict | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=SERIAL,
        version=1,
        minor_version=2,
        data={"host": "192.0.2.1", "username": "u", "password": "p"},
        options=options or {"tibber_home": "Home"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry


def state(hass: HomeAssistant, platform: str, key: str):
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{SERIAL}_{key}")
    assert entity_id, key
    return hass.states.get(entity_id)


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
