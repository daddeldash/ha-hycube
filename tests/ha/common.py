"""Simulated device and helpers shared by the Home Assistant tests."""

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
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


def day_stats(home: float, solar: float | None = None) -> dict:
    """One day of 5-minute statistics with constant powers in W."""
    stats = {
        "Zeitstempel": [
            f"{(i * 5 // 60) % 24:02d}:{i * 5 % 60:02d}:00" for i in range(1, 289)
        ],
        "hausverbrauch": [home] * 288,
    }
    if solar is not None:
        stats["solar1"] = [solar] * 288
    return stats


DAY_STATS = day_stats(600)


def prices(cheap_hours: float) -> dict:
    """Cheap for the next hours, expensive afterwards."""
    start = dt_util.start_of_local_day()
    switch = dt_util.now() + timedelta(hours=cheap_hours)
    rows = []
    for i in range(2 * 96):
        begin = start + timedelta(minutes=15 * i)
        rows.append({"start_time": begin.isoformat(), "price": 0.10 if begin < switch else 0.50})
    return {"prices": {"Home": rows, "Other": [dict(r, price=r["price"] + 1) for r in rows]}}


async def setup(hass: HomeAssistant, options: dict | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=SERIAL,
        version=1,
        minor_version=3,
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
