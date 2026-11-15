"""Schedule ("Fahrplan") for the battery.

Collects the Tibber prices, the PV forecast of the Energy dashboard and the
consumption profile, lets the optimizer find the cheapest battery modes and,
if the user switched that on, sets the mode on the device.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CONTROL_ENABLED,
    CONF_EFFICIENCY,
    CONF_EXPORT_PRICE,
    CONF_GRID_CHARGE_POWER,
    CONF_MAX_CHARGE_SOC,
    CONF_PLAN_GRID_CHARGE,
    CONF_PLAN_HOLD,
    CONF_PV_FACTOR,
    CONF_SHIFT_COST,
    CONF_TIBBER_HOME,
    DEFAULT_EFFICIENCY,
    DEFAULT_EXPORT_PRICE,
    DEFAULT_GRID_CHARGE_POWER,
    DEFAULT_MAX_CHARGE_SOC,
    DEFAULT_PV_FACTOR,
    DEFAULT_SHIFT_COST,
    DOMAIN,
    GRID_CHARGE_AC_EFFICIENCY,
    HOLD_BLOCK_SECONDS,
    HOLD_RELEASE_IDLE,
    MODE_GRID_CHARGE,
    MODE_HOLD,
    MODE_STANDARD,
    PLAN_SLOT_MINUTES,
)
from .optimizer import Battery, Plan, PlannedSlot, Slot, optimize

if TYPE_CHECKING:
    from .coordinator import HyCubeCoordinator

_LOGGER = logging.getLogger(__name__)

TIBBER_DOMAIN = "tibber"
TIBBER_GET_PRICES = "get_prices"
_SLOT_SECONDS = PLAN_SLOT_MINUTES * 60
# A slot that is almost over is not worth planning.
_MIN_SLOT_SECONDS = 60

# PV forecast period: (start timestamp, end timestamp, Wh)
type PvPeriod = tuple[float, float, float]


async def async_tibber_prices(hass: HomeAssistant) -> dict[str, list[tuple[float, float]]]:
    """Prices of today and tomorrow per Tibber home: [(start timestamp, EUR/kWh)]."""
    start = dt_util.start_of_local_day()
    response = await hass.services.async_call(
        TIBBER_DOMAIN,
        TIBBER_GET_PRICES,
        {"start": start.isoformat(), "end": (start + timedelta(days=2)).isoformat()},
        blocking=True,
        return_response=True,
    )
    homes: dict[str, list[tuple[float, float]]] = {}
    for name, rows in ((response or {}).get("prices") or {}).items():
        prices = []
        for row in rows:
            begin = dt_util.parse_datetime(str(row.get("start_time")))
            try:
                price = float(row.get("price"))
            except (TypeError, ValueError):
                continue
            if begin is not None:
                prices.append((begin.timestamp(), price))
        homes[name] = sorted(prices)
    return homes


class HyCubePlanner:
    """Keeps the schedule up to date and carries it out."""

    def __init__(self, hass: HomeAssistant, coordinator: HyCubeCoordinator) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.plan: Plan | None = None
        self.calculated: datetime | None = None
        # Why there is no plan: no_soc, no_profile, no_prices, failed.
        self.error: str | None = None
        self.home: str | None = None  # Tibber home the prices belong to
        self.pv_forecast = False  # a PV forecast was available
        self.battery: Battery | None = None  # model the plan was made with
        self.auto = False  # carry the plan out
        self.hold_blocked_until = 0.0
        self._applied = False  # the device mode was set by the plan
        self._prices: list[tuple[float, float]] = []
        self._warned_home = False
        self._lock = asyncio.Lock()

    # --- persistence ----------------------------------------------------------------
    def restore(self, data: dict[str, Any] | None) -> None:
        """Load what as_dict() stored."""
        data = data or {}
        self.auto = bool(data.get("auto", False))
        self._applied = bool(data.get("applied", False))
        self.hold_blocked_until = float(data.get("hold_blocked_until", 0.0))

    def as_dict(self) -> dict[str, Any]:
        """State that has to survive a restart."""
        return {
            "auto": self.auto,
            "applied": self._applied,
            "hold_blocked_until": self.hold_blocked_until,
        }

    # --- scheduling -------------------------------------------------------------------
    @callback
    def start(self) -> None:
        """Plan now and at the start of every slot."""
        self.coordinator.config_entry.async_on_unload(
            async_track_time_change(
                self.hass,
                self._scheduled_update,
                minute=list(range(0, 60, PLAN_SLOT_MINUTES)),
                second=5,
            )
        )
        self.schedule_update()

    @callback
    def schedule_update(self) -> None:
        """Recalculate in the background."""
        self.coordinator.config_entry.async_create_background_task(
            self.hass, self.async_update(), f"{DOMAIN}_plan"
        )

    async def _scheduled_update(self, _now: datetime) -> None:
        await self.async_update()

    async def async_update(self) -> None:
        """Recalculate the plan and, in automatic mode, act on it."""
        async with self._lock:
            try:
                await self._calculate()
            except Exception:  # noqa: BLE001 - planning must never break polling
                _LOGGER.exception("HyCube schedule calculation failed")
                self.plan = None
                self.error = "failed"
            await self._apply()
        self.coordinator.async_update_listeners()

    # --- inputs -------------------------------------------------------------------------
    @property
    def _options(self) -> Any:
        return self.coordinator.config_entry.options

    async def _calculate(self) -> None:
        coordinator = self.coordinator
        soc = coordinator.soc
        if soc is None:
            self.plan, self.error = None, "no_soc"
            return
        if not coordinator.profile.ready:
            self.plan, self.error = None, "no_profile"
            return
        await self._refresh_prices()
        pv = await self._pv_periods()
        now = dt_util.now()
        slots = self._slots(now, pv)
        if not slots:
            self.plan, self.error = None, "no_prices"
            return
        battery = self.battery_model()
        idle_slots = math.ceil(coordinator.release_idle_seconds() / _SLOT_SECONDS)
        self.plan = await self.hass.async_add_executor_job(
            optimize, slots, soc, battery, coordinator.mode, idle_slots
        )
        self.battery = battery
        self.pv_forecast = pv is not None
        self.calculated = now
        self.error = None

    async def _refresh_prices(self) -> None:
        """Fetch the prices; on failure the last ones stay in use."""
        try:
            homes = await async_tibber_prices(self.hass)
        except (HomeAssistantError, ValueError, KeyError) as err:
            _LOGGER.debug("HyCube schedule: no Tibber prices: %s", err)
            return
        name = await self._pick_home(homes)
        if name is None or not homes[name]:
            return
        self.home = name
        self._prices = homes[name]

    async def _pick_home(self, homes: dict[str, Any]) -> str | None:
        if not homes:
            return None
        wanted = self._options.get(CONF_TIBBER_HOME)
        if wanted in homes:
            return wanted
        if len(homes) == 1 and not wanted:
            return next(iter(homes))
        name = await self._energy_dashboard_home()
        if name not in homes:
            name = sorted(homes)[0]
        if not self._warned_home:
            self._warned_home = True
            _LOGGER.warning(
                "HyCube schedule uses the prices of Tibber home '%s'%s; "
                "choose the home in the HyCube options",
                name,
                f" because '{wanted}' does not exist" if wanted else "",
            )
        return name

    async def _energy_dashboard_home(self) -> str | None:
        """Tibber home whose price sensor the Energy dashboard uses."""
        for source in await self._energy_sources():
            if source.get("type") != "grid":
                continue
            entities = [source.get("entity_energy_price")]
            entities += [f.get("entity_energy_price") for f in source.get("flow_from") or []]
            for entity_id in entities:
                state = self.hass.states.get(entity_id) if entity_id else None
                if state and (name := state.attributes.get("app_nickname")):
                    return str(name)
        return None

    async def _energy_sources(self) -> list[dict[str, Any]]:
        if "energy" not in self.hass.config.components:
            return []
        from homeassistant.components.energy.data import async_get_manager  # noqa: PLC0415

        manager = await async_get_manager(self.hass)
        return list((manager.data or {}).get("energy_sources") or [])

    async def _pv_periods(self) -> list[PvPeriod] | None:
        """PV forecast of the Energy dashboard's solar sources, None without one."""
        try:
            entry_ids = dict.fromkeys(
                entry_id
                for source in await self._energy_sources()
                if source.get("type") == "solar"
                for entry_id in source.get("config_entry_solar_forecast") or []
            )
            if not entry_ids:
                return None
            from homeassistant.components.energy.websocket_api import (  # noqa: PLC0415
                async_get_energy_platforms,
            )

            platforms = await async_get_energy_platforms(self.hass)
            periods: list[PvPeriod] = []
            found = False
            for entry_id in entry_ids:
                entry = self.hass.config_entries.async_get_entry(entry_id)
                if entry is None or entry.domain not in platforms:
                    continue
                forecast = await platforms[entry.domain](self.hass, entry_id)
                if not forecast:
                    continue
                found = True
                for stamp, wh in (forecast.get("wh_hours") or {}).items():
                    begin = dt_util.parse_datetime(str(stamp))
                    if begin is None:
                        continue
                    # Each value covers the time up to the next full hour.
                    end = begin.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
                    periods.append((begin.timestamp(), end.timestamp(), float(wh)))
        except Exception as err:  # noqa: BLE001 - forecast platforms are third-party code
            _LOGGER.debug("HyCube schedule: PV forecast not available: %s", err)
            return None
        return periods if found else None

    def _slots(self, now: datetime, pv: list[PvPeriod] | None) -> list[Slot]:
        """Slots from now to the end of the known prices."""
        prices = self._prices
        if not prices:
            return []
        # A price holds until the next one; the last one as long as its predecessor.
        last = prices[-1][0] - prices[-2][0] if len(prices) > 1 else 3600.0
        end = prices[-1][0] + min(max(last, _SLOT_SECONDS), 3600.0)
        factor = float(self._options.get(CONF_PV_FACTOR, DEFAULT_PV_FACTOR))
        profile = self.coordinator.profile
        current = now.timestamp()
        slot_start = current - current % _SLOT_SECONDS
        index = 0
        slots: list[Slot] = []
        while slot_start < end:
            begin = max(slot_start, current)
            slot_end = slot_start + _SLOT_SECONDS
            slot_start = slot_end
            if slot_end - begin < _MIN_SLOT_SECONDS or prices[0][0] > begin:
                continue
            while index + 1 < len(prices) and prices[index + 1][0] <= begin:
                index += 1
            hours = (slot_end - begin) / 3600
            hour = dt_util.as_local(dt_util.utc_from_timestamp(begin)).replace(
                minute=0, second=0, microsecond=0
            )
            produced = 0.0
            for p_start, p_end, wh in pv or ():
                overlap = min(slot_end, p_end) - max(begin, p_start)
                if overlap > 0 and p_end > p_start:
                    produced += wh * overlap / (p_end - p_start)
            slots.append(
                Slot(
                    start=begin,
                    hours=hours,
                    price=prices[index][1],
                    load=(profile.expected(hour) or 0.0) * hours,
                    pv=produced * factor / 1000,
                )
            )
        return slots

    def battery_model(self) -> Battery:
        """Battery as measured by the integration, with the plan options."""
        coordinator = self.coordinator
        options = self._options
        if override := options.get(CONF_EFFICIENCY):
            efficiency = float(override) / 100
        else:
            efficiency = coordinator.efficiency() or DEFAULT_EFFICIENCY
        return Battery(
            capacity=coordinator.usable_capacity(),
            floor=coordinator.discharge_floor(),
            charge_eff=efficiency,
            grid_eff=efficiency * GRID_CHARGE_AC_EFFICIENCY,
            grid_power=float(options.get(CONF_GRID_CHARGE_POWER, DEFAULT_GRID_CHARGE_POWER))
            / 1000,
            max_grid_soc=float(options.get(CONF_MAX_CHARGE_SOC, DEFAULT_MAX_CHARGE_SOC)),
            export_price=float(options.get(CONF_EXPORT_PRICE, DEFAULT_EXPORT_PRICE)) / 100,
            shift_cost=float(options.get(CONF_SHIFT_COST, DEFAULT_SHIFT_COST)) / 100,
            release_slots=math.ceil(HOLD_RELEASE_IDLE / _SLOT_SECONDS),
            allow_hold=bool(options.get(CONF_PLAN_HOLD, True))
            and not self.hold_blocked
            and bool(coordinator.command_for(MODE_HOLD)),
            allow_grid_charge=bool(options.get(CONF_PLAN_GRID_CHARGE, True))
            and bool(coordinator.command_for(MODE_GRID_CHARGE)),
        )

    # --- reading the plan -------------------------------------------------------------
    @property
    def hold_blocked(self) -> bool:
        """Hold is left out because the device refilled the reserve from the grid."""
        return time.time() < self.hold_blocked_until

    def upcoming(self) -> list[PlannedSlot]:
        """Slots of the plan that are not over yet."""
        if self.plan is None:
            return []
        now = time.time()
        return [s for s in self.plan.slots if s.start + s.hours * 3600 > now]

    def current_slot(self) -> PlannedSlot | None:
        """Slot of the plan that covers now."""
        return next(iter(self.upcoming()), None)

    def wanted(self) -> tuple[str, int]:
        """Mode the plan asks for now and the target SoC for grid charging."""
        coordinator = self.coordinator
        slots = self.upcoming()
        if not slots or slots[0].mode != MODE_GRID_CHARGE:
            return (slots[0].mode if slots else MODE_STANDARD), coordinator.target_soc
        reached = slots[0].soc
        for slot in slots:
            if slot.mode != MODE_GRID_CHARGE:
                break
            reached = slot.soc
        target = min(100, max(10, math.ceil(reached - 1e-6)))
        soc = coordinator.soc
        if soc is not None and target <= soc:
            return MODE_STANDARD, coordinator.target_soc
        return MODE_GRID_CHARGE, target

    # --- acting on the plan ---------------------------------------------------------
    def _control_enabled(self) -> bool:
        return bool(self._options.get(CONF_CONTROL_ENABLED, True))

    async def _apply(self) -> None:
        """Put the device into the mode the plan asks for."""
        coordinator = self.coordinator
        if not self.auto or not self._control_enabled():
            return
        if self.plan is None:
            # Without a plan only undo what the plan itself has set.
            if self._applied and coordinator.mode != MODE_STANDARD:
                await self._set_mode(MODE_STANDARD)
            return
        mode, target = self.wanted()
        if mode == coordinator.mode and (
            mode != MODE_GRID_CHARGE or abs(coordinator.target_soc - target) < 2
        ):
            return
        if mode == MODE_GRID_CHARGE:
            coordinator.target_soc = target
        await self._set_mode(mode)

    async def _set_mode(self, mode: str) -> None:
        try:
            await self.coordinator.async_set_mode(mode)
        except HomeAssistantError as err:
            _LOGGER.warning("HyCube schedule could not set mode '%s': %s", mode, err)
            return
        self._applied = mode != MODE_STANDARD
        self.coordinator.async_schedule_save()
        _LOGGER.info("HyCube schedule set mode '%s'", mode)

    async def async_set_auto(self, enabled: bool) -> None:
        """Switch carrying out the plan on or off."""
        self.auto = enabled
        self.coordinator.async_schedule_save()
        if enabled:
            await self.async_update()
            return
        async with self._lock:
            if (
                self._applied
                and self.coordinator.mode != MODE_STANDARD
                and self._control_enabled()
            ):
                await self._set_mode(MODE_STANDARD)
            self._applied = False
        self.coordinator.async_schedule_save()
        self.coordinator.async_update_listeners()

    @callback
    def block_hold(self) -> None:
        """Stop planning "hold": the device refills the reserve from the grid."""
        if self.hold_blocked:
            return
        self.hold_blocked_until = time.time() + HOLD_BLOCK_SECONDS
        _LOGGER.warning(
            "HyCube schedule leaves out mode 'hold' for %d h because the device "
            "charged the battery from the grid while holding",
            HOLD_BLOCK_SECONDS // 3600,
        )
        self.coordinator.async_schedule_save()
        self.schedule_update()
