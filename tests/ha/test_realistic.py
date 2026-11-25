"""Measured efficiency, the dropped standby default and the profile source switch."""

from datetime import timedelta

import pytest
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from common import DOMAIN, SERIAL, setup, state


def entry_with(hass_storage, options: dict, minor_version: int = 3, days: dict | None = None):
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=SERIAL, version=1, minor_version=minor_version,
        data={"host": "192.0.2.1", "username": "u", "password": "p"},
        options=options,
    )
    if days is not None:
        key = f"{DOMAIN}.{entry.entry_id}.history"
        hass_storage[key] = {"version": 1, "key": key, "data": {"days": days}}
    return entry


async def start(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_old_standby_suggestion_is_dropped(hass: HomeAssistant, hass_storage, api, tibber) -> None:
    entry = entry_with(
        hass_storage, {"tibber_home": "Home", "standby_power": 50, "shift_cost": 1.0}, minor_version=2
    )
    await start(hass, entry)
    assert entry.minor_version == 3
    assert "standby_power" not in entry.options
    assert "shift_cost" not in entry.options
    model = entry.runtime_data.planner.battery_model()
    assert model.standby == 0.0
    assert model.shift_cost == 0.0


async def test_own_standby_value_stays(hass: HomeAssistant, hass_storage, api, tibber) -> None:
    entry = entry_with(
        hass_storage, {"tibber_home": "Home", "standby_power": 20, "shift_cost": 2.0}, minor_version=2
    )
    await start(hass, entry)
    assert entry.options["standby_power"] == 20
    assert entry.options["shift_cost"] == 2.0
    assert entry.runtime_data.planner.battery_model().standby == 0.02


async def test_efficiency_from_the_counters(hass: HomeAssistant, api, tibber) -> None:
    entry = await setup(hass)
    coordinator = entry.runtime_data
    planner = coordinator.planner
    registry = er.async_get(hass)
    sensor = lambda key: registry.async_get_entity_id("sensor", DOMAIN, f"{SERIAL}_{key}")  # noqa: E731
    midnight = dt_util.start_of_local_day()
    first = midnight - timedelta(days=30)

    # 30 days: 3 kWh charged and 2.25 kWh discharged a day, 15 % at every midnight.
    for key, per_day in (("energy_battery_charge", 3.0), ("energy_battery_discharge", 2.25)):
        rows, total, hour = [], 0.0, first
        while hour < midnight:
            total += per_day / 24
            rows.append({"start": dt_util.as_utc(hour), "state": total, "sum": total})
            hour += timedelta(hours=1)
        async_import_statistics(
            hass,
            {
                "mean_type": StatisticMeanType.NONE, "has_sum": True, "name": None,
                "source": "recorder", "statistic_id": sensor(key),
                "unit_class": "energy", "unit_of_measurement": "kWh",
            },
            rows,
        )
    rows, hour = [], first
    while hour <= midnight:
        rows.append({"start": dt_util.as_utc(hour), "mean": 15.0, "min": 15.0, "max": 15.0})
        hour += timedelta(hours=1)
    async_import_statistics(
        hass,
        {
            "mean_type": StatisticMeanType.ARITHMETIC, "has_sum": False, "name": None,
            "source": "recorder", "statistic_id": sensor("battery_soc"),
            "unit_class": None, "unit_of_measurement": "%",
        },
        rows,
    )
    await async_wait_recording_done(hass)

    assert planner.efficiency_caution() == 0.9  # nothing measured yet
    await coordinator.async_rebuild_profile()
    await hass.async_block_till_done(wait_background_tasks=True)
    measured = coordinator.counter_efficiency
    assert measured is not None
    assert measured.efficiency == pytest.approx(0.75, abs=0.01)
    assert coordinator.efficiency() == measured.efficiency
    assert planner.efficiency_measured()
    assert planner.efficiency_caution() == 0.95
    assert planner.battery_model().charge_eff == measured.efficiency
    attributes = state(hass, "sensor", "efficiency_estimate").attributes
    assert attributes["source"] == "counters"
    assert attributes["decided_with"] == pytest.approx(75 * 0.95, abs=1)
    assert coordinator._store_data()["counter_efficiency"]["efficiency"] == measured.efficiency

    # A value set in the options wins and is treated as unmeasured.
    hass.config_entries.async_update_entry(entry, options={"tibber_home": "Home", "efficiency": 80})
    await hass.async_block_till_done(wait_background_tasks=True)
    planner = entry.runtime_data.planner
    assert planner.battery_model().charge_eff == 0.8
    assert planner.efficiency_caution() == 0.9
    assert state(hass, "sensor", "efficiency_estimate").attributes["set_in_options"] is True


async def test_forecasts_from_another_source_are_not_judged(
    hass: HomeAssistant, hass_storage, api, tibber
) -> None:
    today = dt_util.now().date()
    days = {}
    for age in range(1, 11):
        record = {"home": [1.0] * 24, "pred": [1.1] * 24}  # 10 % off
        if age == 1:
            record["pred"] = [0.04] * 24  # made from a wrong source, before tags existed
        if age == 2:
            record["pred"], record["pred_src"] = [3.0] * 24, "sensor.something_else"
        if age == 3:
            record["pred_src"] = "device"
        days[(today - timedelta(days=age)).isoformat()] = record
    days[today.isoformat()] = {"pred": [0.04] * 24}  # today's, made from the wrong source
    entry = entry_with(hass_storage, {"tibber_home": "Home"}, days=days)
    await start(hass, entry)

    error = state(hass, "sensor", "consumption_forecast_error")
    assert float(error.state) == pytest.approx(10.0)
    assert error.attributes["days"] == 8
    # Today's forecast was replaced by one from the current source.
    archive = entry.runtime_data.archive
    assert archive.value(today, "pred_src") == "device"
    assert archive.snapshot(today, "pred")[0] > 0.04
    # It stays put for the rest of the day.
    first = list(archive.snapshot(today, "pred"))
    await entry.runtime_data.forecaster.async_refresh()
    assert archive.snapshot(today, "pred") == first


async def test_button_after_changing_the_source(hass: HomeAssistant, api, tibber) -> None:
    entry = await setup(hass)
    coordinator = entry.runtime_data
    today = dt_util.now().date()
    assert coordinator.archive.value(today, "pred_src") == "device"
    # The new source has a history of its own: 0.8 kWh an hour for three weeks.
    source = "sensor.hycube_home_consumption_energy"
    rows, total, hour = [], 0.0, dt_util.start_of_local_day() - timedelta(days=21)
    while hour < dt_util.start_of_local_day():
        total += 0.8
        rows.append({"start": dt_util.as_utc(hour), "state": total, "sum": total})
        hour += timedelta(hours=1)
    async_import_statistics(
        hass,
        {
            "mean_type": StatisticMeanType.NONE, "has_sum": True, "name": None,
            "source": "recorder", "statistic_id": source,
            "unit_class": "energy", "unit_of_measurement": "kWh",
        },
        rows,
    )
    await async_wait_recording_done(hass)
    hass.config_entries.async_update_entry(
        entry, options={"tibber_home": "Home", "profile_source": source}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    coordinator = entry.runtime_data
    button = er.async_get(hass).async_get_entity_id("button", DOMAIN, f"{SERIAL}_rebuild_profile")
    await hass.services.async_call("button", "press", {"entity_id": button}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert coordinator.profile_source() == "sensor.hycube_home_consumption_energy"
    assert coordinator.archive.value(today, "pred_src") == "sensor.hycube_home_consumption_energy"
    assert coordinator.planner.calculated is not None
    # The forecast now rests on the new source: 0.8 kWh an hour instead of 0.6.
    assert sum(coordinator.archive.snapshot(today, "pred")) == pytest.approx(19.2, rel=0.01)
    assert float(state(hass, "sensor", "forecast_tomorrow").state) == pytest.approx(19.2, rel=0.01)
