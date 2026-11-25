"""Quiet battery-empty sensor, measured powers, audit of the schedule."""

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from common import DOMAIN, VALUES, day_stats, setup, state


async def poll(hass: HomeAssistant, entry: MockConfigEntry, times: int = 1) -> None:
    for _ in range(times):
        await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


async def test_battery_empty_sensor_keeps_still(hass: HomeAssistant, api, tibber) -> None:
    # At rest after discharging: 18 % with the reserve at 15 %.
    api.get_values.return_value = dict(VALUES, Battery_C="18")
    entry = await setup(hass)
    first = state(hass, "sensor", "battery_empty_estimate")
    assert first.attributes["empty"] is True
    assert first.attributes["empty_below_soc"] == 20
    assert first.attributes["usable_energy_kwh"] == 0
    for soc in ("18", "17", "19", "20", "18"):  # the reading wanders a little
        api.get_values.return_value = dict(VALUES, Battery_C=soc)
        await poll(hass, entry, 3)
        now = state(hass, "sensor", "battery_empty_estimate")
        assert now.state == first.state
        assert now.last_changed == first.last_changed

    # Charged: an estimate in the future, on a quarter of an hour.
    api.get_values.return_value = dict(VALUES, Battery_C="60")
    await poll(hass, entry)
    charged = state(hass, "sensor", "battery_empty_estimate")
    assert charged.attributes["empty"] is False
    when = dt_util.parse_datetime(charged.state)
    assert when > dt_util.utcnow()
    assert when.minute % 15 == 0 and when.second == 0
    # A percent less moves the estimate by minutes only: nothing is reported.
    api.get_values.return_value = dict(VALUES, Battery_C="59.9")
    await poll(hass, entry, 3)
    assert state(hass, "sensor", "battery_empty_estimate").state == charged.state

    # The schedule does not count on the rest above the reserve either.
    api.get_values.return_value = dict(VALUES, Battery_C="19")
    await poll(hass, entry)
    await entry.runtime_data.planner.async_update()
    slots = entry.runtime_data.planner.plan.slots
    assert slots[0].source != "battery" or slots[0].mode != "standard"


async def test_peak_powers_are_measured_and_used(hass: HomeAssistant, api, tibber) -> None:
    entry = await setup(hass)
    coordinator = entry.runtime_data
    planner = coordinator.planner
    assert coordinator.charge_power() is None
    assert planner.grid_charge_power() == 2500  # the option's default

    # A spike is not enough; a minute at 3.3 kW is.
    coordinator._recent_power.clear()  # samples of the polls so far
    coordinator._track_peaks(1000.0, 5000.0)
    coordinator._track_peaks(1010.0, 3300.0)
    for t in range(1020, 1080, 10):
        coordinator._track_peaks(float(t), 3350.0)
    assert coordinator.peak_charge == 3300.0
    for t in range(2000, 2070, 10):
        coordinator._track_peaks(float(t), -3600.0)
    assert coordinator.peak_discharge == 3600.0
    coordinator._recent_power.clear()
    # Trickle charging never counts as the charge power.
    assert coordinator.charge_power() == 3300.0
    assert planner.grid_charge_power() == pytest.approx(3300 / 0.95)
    model = planner.battery_model()
    assert model.grid_power == pytest.approx(3.3 / 0.95)
    assert model.discharge_power == 3.6
    assert model.standby == 0.0

    await planner.async_update()
    attributes = state(hass, "sensor", "plan_next_grid_charge").attributes
    assert attributes["peak_charge_w"] == 3300
    assert attributes["peak_discharge_w"] == 3600
    assert attributes["charge_power_w"] == round(3300 / 0.95)
    assert coordinator._store_data()["peak_charge"] == 3300.0


async def test_peak_powers_start_from_the_statistics(hass: HomeAssistant, api, tibber) -> None:
    from homeassistant.components.recorder.models import StatisticMeanType
    from homeassistant.components.recorder.statistics import async_import_statistics
    from homeassistant.helpers import entity_registry as er
    from pytest_homeassistant_custom_component.components.recorder.common import (
        async_wait_recording_done,
    )

    entry = await setup(hass)
    registry = er.async_get(hass)
    hour = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(days=3)
    for key, peak in (("battery_charge_power", 3362.0), ("battery_discharge_power", 3606.0)):
        async_import_statistics(
            hass,
            {
                "mean_type": StatisticMeanType.ARITHMETIC,
                "has_sum": False,
                "name": None,
                "source": "recorder",
                "statistic_id": registry.async_get_entity_id("sensor", DOMAIN, f"SN123_{key}"),
                "unit_class": "power",
                "unit_of_measurement": "W",
            },
            [
                {"start": hour, "mean": 100.0, "min": 0.0, "max": 800.0},
                {"start": hour + timedelta(hours=1), "mean": 900.0, "min": 0.0, "max": peak},
            ],
        )
    await async_wait_recording_done(hass)
    await entry.runtime_data.async_rebuild_profile()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data.peak_charge == 3362.0
    assert entry.runtime_data.peak_discharge == 3606.0


async def test_archive_keeps_what_the_audit_needs(hass: HomeAssistant, api, tibber) -> None:
    stats = day_stats(600, 200)
    stats["Netzbezug"] = [-400] * 288  # the firmware reports some flows negative
    stats["Einspeisung"] = [0] * 288
    api.get_day_stats.return_value = stats
    entry = await setup(hass)
    archive = entry.runtime_data.archive
    today = dt_util.now().date()
    record = archive.days[(today - timedelta(days=1)).isoformat()]
    assert record["grid"] == [pytest.approx(0.4)] * 24
    assert record["export"] == [0.0] * 24
    # Hourly prices of today, from the quarter-hour prices.
    prices = archive.value(today, "price")
    assert len(prices) == 24
    assert min(prices) == pytest.approx(0.10) and max(prices) == pytest.approx(0.50)
    assert archive.value(today, "active") is None
    await entry.runtime_data.async_set_mode("hold")
    await entry.runtime_data.planner.async_update()
    assert archive.value(today, "active") is True


def audited_entry(hass_storage, saved_active: float) -> MockConfigEntry:
    """An entry whose archive holds ten quiet days and four days with a deviation.

    600 W consumption all day at 0.30/0.45 EUR, battery empty. On quiet days
    the grid delivered exactly that; on the active days it delivered
    `saved_active` EUR less than that would have cost.
    """
    today = dt_util.now().date()
    prices = [0.30] * 12 + [0.45] * 12
    days = {}
    for age in range(1, 16):
        record = {
            "home": [0.6] * 24, "solar": [0.0] * 24, "grid": [0.6] * 24, "export": [0.0] * 24,
            "price": prices, "soc": 15,
        }
        if age <= 4:
            record["active"] = True
            record["grid"] = [0.6] * 23 + [0.6 - saved_active / 0.45]
        days[(today - timedelta(days=age)).isoformat()] = record
    days[today.isoformat()] = {"soc": 15}
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="SN123", version=1, minor_version=3,
        data={"host": "192.0.2.1", "username": "u", "password": "p"},
        options={"tibber_home": "Home"},
    )
    hass_storage[f"{DOMAIN}.{entry.entry_id}.history"] = {
        "version": 1, "key": f"{DOMAIN}.{entry.entry_id}.history", "data": {"days": days},
    }
    return entry


async def test_audit_shows_what_the_schedule_saved(
    hass: HomeAssistant, hass_storage, api, tibber
) -> None:
    entry = audited_entry(hass_storage, saved_active=0.20)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    realized = state(hass, "sensor", "plan_realized_savings")
    assert float(realized.state) == pytest.approx(0.80)
    assert realized.attributes["active_days"] == 4
    assert realized.attributes["model_error_per_day"] == pytest.approx(0.0)
    assert realized.attributes["paused"] is False
    planner = entry.runtime_data.planner
    assert planner.battery_model().allow_grid_charge is True
    # Cheap now, dear later: the schedule acts, and tells what it rests on.
    assert state(hass, "sensor", "plan_source").state == "grid_charge"
    savings = state(hass, "sensor", "plan_savings")
    assert 0.05 <= float(savings.state) <= savings.attributes["expected"]


async def test_losses_put_the_schedule_back_to_standard(
    hass: HomeAssistant, hass_storage, api, tibber, caplog
) -> None:
    entry = audited_entry(hass_storage, saved_active=-0.20)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    realized = state(hass, "sensor", "plan_realized_savings")
    assert float(realized.state) == pytest.approx(-0.80)
    assert realized.attributes["paused"] is True
    assert "cost 0.80 more than standard operation on 4 days" in caplog.text

    planner = entry.runtime_data.planner
    assert planner.paused
    model = planner.battery_model()
    assert not model.allow_grid_charge and not model.allow_hold
    # The same prices that made the schedule act in the test above.
    source = state(hass, "sensor", "plan_source")
    assert source.attributes["paused"] is True
    assert {s.mode for s in planner.plan.slots} == {"standard"}
    assert state(hass, "sensor", "plan_mode").state == "standard"
