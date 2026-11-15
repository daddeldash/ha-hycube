"""Archive, consumption model and PV correction inside Home Assistant."""

from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from common import DOMAIN, day_stats, setup, state

SENSOR = "sensor.outdoor"
WEATHER = "weather.home"
OPTIONS = {"tibber_home": "Home", "temperature_entity": SENSOR, "weather_entity": WEATHER}


def outdoor(day: date) -> float:
    """Getting colder: 6 deg C yesterday, 0.25 K warmer each day before, with weather on top."""
    age = (dt_util.now().date() - day).days
    return 6.0 + age * 0.25 + (2.0 if age % 3 == 0 else -1.0)


def heat_pump_watts(day: date) -> float:
    """300 W base load plus 100 W per K below 15 deg C."""
    return 300.0 + 100.0 * max(15.0 - outdoor(day), 0.0)


@pytest.fixture
def cold_house(hass: HomeAssistant, api):
    """A home whose consumption follows the outdoor temperature, with sensor and weather service."""
    api.get_day_stats.side_effect = lambda day: day_stats(heat_pump_watts(day))
    api.get_day_stats.return_value = None

    async def statistics(self, entity_id, start, end, period, *args):
        if entity_id != SENSOR or period != "day":
            return []
        rows, day = [], start
        while day < end:
            rows.append((day, outdoor(day.date())))
            day += timedelta(days=1)
        return rows

    # The weather service runs 4 K below the sensor on the house wall.
    hass.states.async_set(SENSOR, "6.0", {"device_class": "temperature"})
    hass.states.async_set(WEATHER, "cloudy", {"temperature": 2.0})

    async def get_forecasts(call: ServiceCall) -> dict:
        start = dt_util.start_of_local_day()
        if call.data["type"] == "daily":
            return {WEATHER: {"forecast": []}}
        forecast = [
            {"datetime": (start + timedelta(hours=h)).isoformat(), "temperature": -3.0}
            for h in range(72)
        ]
        return {WEATHER: {"forecast": forecast}}

    hass.services.async_register(
        "weather", "get_forecasts", get_forecasts, supports_response=SupportsResponse.ONLY
    )
    with patch(
        "custom_components.hycube.forecaster.ConsumptionForecaster._statistics", statistics
    ):
        yield api


async def rebuild(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await entry.runtime_data.async_rebuild_profile()
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_archive_grows_back_until_the_device_has_no_more(
    hass: HomeAssistant, hass_storage, api, tibber
) -> None:
    today = dt_util.now().date()
    has_data = lambda day: (today - day).days <= 40  # noqa: E731
    api.get_day_stats.side_effect = lambda day: day_stats(600, 200) if has_data(day) else {}
    entry = await setup(hass)
    archive = entry.runtime_data.archive
    assert archive.count() == 30  # one run
    assert not archive.complete

    await rebuild(hass, entry)
    assert archive.count() == 40
    assert archive.complete
    assert archive.oldest() == today - timedelta(days=40)
    calls = api.get_day_stats.call_count
    await rebuild(hass, entry)
    assert api.get_day_stats.call_count == calls  # nothing left to ask for

    # Consumption and PV production per hour, in a store of its own.
    stored = hass_storage[f"{DOMAIN}.{entry.entry_id}.history"]["data"]
    record = stored["days"][(today - timedelta(days=1)).isoformat()]
    assert record["home"] == [pytest.approx(0.6)] * 24
    assert record["solar"] == [pytest.approx(0.2)] * 24
    assert stored["complete"] is True
    assert "history" not in entry.runtime_data._store_data()
    assert state(hass, "sensor", "consumption_model").attributes["archive_days"] == 40


async def test_cache_of_earlier_versions_is_taken_over(
    hass: HomeAssistant, hass_storage, api, tibber
) -> None:
    today = dt_util.now().date()
    old = {(today - timedelta(days=d)).isoformat(): [0.5] * 24 for d in range(1, 57)}
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="SN123", version=1, minor_version=2,
        data={"host": "192.0.2.1", "username": "u", "password": "p"},
        options={"tibber_home": "Home"},
    )
    hass_storage[f"{DOMAIN}.{entry.entry_id}"] = {
        "version": 1, "key": f"{DOMAIN}.{entry.entry_id}", "data": {"history": old},
    }
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    archive = entry.runtime_data.archive
    assert archive.count() == 56 + 30  # the old weeks plus one run further back
    assert archive.days[(today - timedelta(days=1)).isoformat()]["home"][0] == 0.5
    asked = {call.args[0] for call in api.get_day_stats.call_args_list}
    assert max(asked) == today - timedelta(days=57)


async def test_temperature_model_is_chosen_and_used(
    hass: HomeAssistant, cold_house, tibber
) -> None:
    entry = await setup(hass, OPTIONS)
    await rebuild(hass, entry)  # 60 days in the archive
    coordinator = entry.runtime_data
    forecaster = coordinator.forecaster
    today = dt_util.now().date()

    assert forecaster.kind == "temperature"
    model = state(hass, "sensor", "consumption_model")
    assert model.state == "temperature"
    errors = model.attributes["backtest_daily_error"]
    assert errors["temperature"] < 3 < errors["recent"]
    assert model.attributes["base_temperature"] == 15
    assert model.attributes["kwh_per_kelvin"] == pytest.approx(2.4, abs=0.1)
    assert model.attributes["base_load_kwh"] == pytest.approx(7.2, abs=0.3)
    assert model.attributes["archive_days"] == 60
    accuracy = state(hass, "sensor", "consumption_model_accuracy")
    assert float(accuracy.state) > 97
    assert accuracy.attributes["model"] == "temperature"
    assert accuracy.attributes["all_models"]["recent"] < 75
    assert accuracy.attributes["measured_accuracy"] is None

    # Forecast of -3 deg C plus the 4 K the sensor reads above the service.
    assert forecaster.offset == pytest.approx(4.0)
    tomorrow = today + timedelta(days=1)
    assert model.attributes["temperatures"][tomorrow.isoformat()] == pytest.approx(1.0)
    expected = (300 + 100 * 14) * 24 / 1000  # colder than any day in the archive
    start = datetime.combine(tomorrow, datetime.min.time(), tzinfo=dt_util.get_default_time_zone())
    day = sum(coordinator.profile.expected(start + timedelta(hours=h)) for h in range(24))
    assert day == pytest.approx(expected, rel=0.05)
    assert float(state(hass, "sensor", "forecast_tomorrow").state) == pytest.approx(expected, rel=0.05)

    # The schedule plans with it, and today's forecast is kept to be judged later.
    slot = next(s for s in coordinator.planner.plan.slots if s.hours == 0.25)
    assert slot.load == pytest.approx(expected / 96, rel=0.1)
    assert len(coordinator.archive.snapshot(today, "pred")) == 24
    assert state(hass, "sensor", "forecast_next_24h").attributes["model"] == "temperature"


async def test_without_weather_the_temperature_model_is_not_used(
    hass: HomeAssistant, cold_house, tibber
) -> None:
    entry = await setup(hass, {"tibber_home": "Home", "temperature_entity": SENSOR})
    await rebuild(hass, entry)
    forecaster = entry.runtime_data.forecaster
    assert forecaster.kind in ("recent", "seasonal")
    assert "temperature" not in forecaster.backtest.hourly
    assert forecaster.temperatures == {}
    assert entry.runtime_data.profile.hours  # still a forecast per hour


async def test_measured_forecast_error(hass: HomeAssistant, hass_storage, api, tibber) -> None:
    today = dt_util.now().date()
    days = {}
    for age in range(1, 11):
        days[(today - timedelta(days=age)).isoformat()] = {
            "home": [0.6] * 24,
            "pred": [0.75] * 24,  # 25 % too high
        }
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="SN123", version=1, minor_version=2,
        data={"host": "192.0.2.1", "username": "u", "password": "p"},
        options={"tibber_home": "Home"},
    )
    hass_storage[f"{DOMAIN}.{entry.entry_id}.history"] = {
        "version": 1, "key": f"{DOMAIN}.{entry.entry_id}.history", "data": {"days": days},
    }
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    error = state(hass, "sensor", "consumption_forecast_error")
    assert float(error.state) == 25.0
    assert error.attributes["days"] == 10


async def test_pv_forecast_is_corrected(hass: HomeAssistant, hass_storage, api, tibber) -> None:
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
    # For three weeks the plant produced half of what was forecast.
    today = dt_util.now().date()
    days = {
        (today - timedelta(days=age)).isoformat(): {
            "home": [0.6] * 24, "solar": [0.5] * 24, "pv_fc": [1.0] * 24,
        }
        for age in range(1, 22)
    }
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="SN123", version=1, minor_version=2,
        data={"host": "192.0.2.1", "username": "u", "password": "p"},
        options={"tibber_home": "Home"},
    )
    hass_storage[f"{DOMAIN}.{entry.entry_id}.history"] = {
        "version": 1, "key": f"{DOMAIN}.{entry.entry_id}.history", "data": {"days": days},
    }
    hour = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)

    async def forecast(hass: HomeAssistant, entry_id: str) -> dict:
        return {"wh_hours": {(hour + timedelta(hours=h)).isoformat(): 4000 for h in range(1, 4)}}

    entry.add_to_hass(hass)
    with patch(
        "homeassistant.components.energy.websocket_api.async_get_energy_platforms",
        AsyncMock(return_value={"fake_forecast": forecast}),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        coordinator = entry.runtime_data
        correction = state(hass, "sensor", "pv_forecast_correction")
        assert float(correction.state) == pytest.approx(0.5, abs=0.01)
        assert correction.attributes["days"] == 21
        assert correction.attributes["used_for_schedule"] is True
        # 3 h x 4 kWh forecast, halved.
        assert sum(s.pv for s in coordinator.planner.plan.slots) == pytest.approx(6.0, rel=0.03)
        # Today's forecast is kept, uncorrected, for the comparison to come.
        kept = coordinator.archive.snapshot(today, "pv_fc")
        assert kept is not None and max(kept) == pytest.approx(4.0)

        # Switched off: the forecast is used as it is.
        hass.config_entries.async_update_entry(
            entry, options={"tibber_home": "Home", "pv_auto_correct": False}
        )
        await hass.async_block_till_done(wait_background_tasks=True)
        coordinator = entry.runtime_data
        assert sum(s.pv for s in coordinator.planner.plan.slots) == pytest.approx(12.0, rel=0.03)
        assert state(hass, "sensor", "pv_forecast_correction").attributes["used_for_schedule"] is False


async def test_device_options(hass: HomeAssistant, api, tibber) -> None:
    entry = await setup(hass, {"tibber_home": "Home", "profile_weeks": 8})
    hass.states.async_set(SENSOR, "6.0", {"device_class": "temperature"})
    hass.states.async_set(WEATHER, "cloudy", {"temperature": 2.0})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    assert result["type"] is FlowResultType.FORM
    fields = {str(key) for key in result["data_schema"].schema}
    assert {"temperature_entity", "weather_entity"} <= fields
    assert "profile_weeks" not in fields
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "scan_interval": 10,
            "status_interval": 60,
            "battery_capacity": 6.6,
            "control_enabled": True,
            "temperature_entity": SENSOR,
            "weather_entity": WEATHER,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.options["temperature_entity"] == SENSOR
    assert entry.options["weather_entity"] == WEATHER
    assert entry.options["tibber_home"] == "Home"  # other step untouched
    assert "profile_weeks" not in entry.options  # no longer an option


async def test_removing_the_entry_deletes_its_stores(
    hass: HomeAssistant, hass_storage, api, tibber
) -> None:
    entry = await setup(hass)
    await entry.runtime_data.async_shutdown()
    keys = (f"{DOMAIN}.{entry.entry_id}", f"{DOMAIN}.{entry.entry_id}.history")
    assert all(key in hass_storage for key in keys)
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert not any(key in hass_storage for key in keys)


async def test_temperatures_come_from_long_term_statistics(
    hass: HomeAssistant, api, tibber
) -> None:
    """The real recorder path: hourly means of the sensor become local daily means."""
    from homeassistant.components.recorder.models import StatisticMeanType
    from homeassistant.components.recorder.statistics import async_import_statistics
    from pytest_homeassistant_custom_component.components.recorder.common import (
        async_wait_recording_done,
    )

    midnight = dt_util.start_of_local_day()
    rows = []
    for age in range(1, 4):  # the last three days: 1, 2 and 3 deg C, +-2 K over the day
        for hour in range(24):
            start = midnight - timedelta(days=age) + timedelta(hours=hour)
            rows.append({"start": dt_util.as_utc(start), "mean": age + (2.0 if hour < 12 else -2.0)})
    async_import_statistics(
        hass,
        {
            "mean_type": StatisticMeanType.ARITHMETIC,
            "has_sum": False,
            "name": None,
            "source": "recorder",
            "statistic_id": SENSOR,
            "unit_class": "temperature",
            "unit_of_measurement": "°C",
        },
        rows,
    )
    await async_wait_recording_done(hass)

    entry = await setup(hass, {"tibber_home": "Home", "temperature_entity": SENSOR})
    archive = entry.runtime_data.archive
    today = dt_util.now().date()
    for age in range(1, 4):
        assert archive.temp(today - timedelta(days=age)) == pytest.approx(age)
    assert archive.temp(today - timedelta(days=4)) is None  # no statistics that far back
    hourly = await entry.runtime_data.forecaster._statistics(
        SENSOR, midnight - timedelta(days=1), midnight, "hour"
    )
    assert len(hourly) == 24
    assert hourly[0] == (midnight - timedelta(days=1), pytest.approx(3.0))
