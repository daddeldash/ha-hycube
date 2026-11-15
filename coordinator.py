"""Data update coordinator for HyCube."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import HyCubeApi, HyCubeAuthError, HyCubeBusyError, HyCubeError
from .archive import Archive
from .const import (
    ARCHIVE_DAYS_PER_RUN,
    BATTERY_ACTIVE_DEFAULT,
    BATTERY_PROTECTION,
    BUSY_BACKOFF,
    CONF_BATTERY_CAPACITY,
    CONF_CONTROL_ENABLED,
    CONF_PROFILE_SOURCE,
    CONF_SCAN_INTERVAL,
    CONF_STATUS_INTERVAL,
    DEFAULT_BATTERY_CAPACITY,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_STATUS_INTERVAL,
    DEFAULT_TARGET_SOC,
    DOMAIN,
    EFFICIENCY_MIN_SAMPLES,
    ENERGY_BATTERY_CHARGE,
    ENERGY_BATTERY_DISCHARGE,
    ENERGY_GRID_EXPORT,
    ENERGY_GRID_IMPORT,
    ENERGY_HOME,
    ENERGY_SOLAR,
    GAP_BACKFILL_FACTOR,
    GAP_MAX_INTERPOLATE,
    GAP_MIN_BACKFILL,
    HISTORY_REQUEST_SPACING,
    HOLD_GRID_CHARGE_WARN,
    HOLD_MIN_ACTIVE,
    HOLD_PV_IDLE,
    HOLD_RELEASE_GRID_MIN,
    HOLD_RELEASE_IDLE,
    HOLD_RELEASE_WARN_AFTER,
    HOLD_RELEASE_WATCH,
    HOLD_RESEND_MIN_INTERVAL,
    HOLD_RESEND_STEP,
    MAX_BACKOFF,
    MODE_COMMAND_DEFAULT,
    MODE_COMMAND_OPTION,
    MODE_GRID_CHARGE,
    MODE_HOLD,
    MODE_STANDARD,
    MODES,
    PEAK_MIN_POWER,
    PEAK_WINDOW,
    STALE_TOLERANCE,
    STORAGE_SAVE_DELAY,
    STORAGE_VERSION,
)
from .energy import (
    CapacityEstimator,
    EnergyAccumulator,
    buckets_from_day_stats,
    hourly_kwh,
    powers_from_values,
    round_trip_efficiency,
    to_float,
)
from .forecaster import RECORDER_YEARS, ConsumptionForecaster
from .planner import HyCubePlanner
from .profile import ConsumptionProfile

_LOGGER = logging.getLogger(__name__)

# Transient /db_today/ errors: keep the gap open for this many polls, then give up.
BACKFILL_RETRIES = 3
# /db_today/ is fetched per day; longer outages are only filled for the last days.
BACKFILL_MAX_DAYS = 7
# The controller may need a moment before /data_row/ reflects a new mode.
MODE_READBACK_GRACE = 120.0
# Normal-operation share in a /Bat/setCustomBat/ request.
_SPLIT_ACTIVE = re.compile(r"setCustomBat/\S*[?&]x_active=(\d+)")

type HyCubeConfigEntry = ConfigEntry[HyCubeCoordinator]


@dataclass
class HyCubeData:
    """Snapshot handed to the entities."""

    values: dict[str, Any] = field(default_factory=dict)
    status: dict[str, Any] = field(default_factory=dict)
    sample_time: datetime | None = None
    stale: bool = False


class HyCubeCoordinator(DataUpdateCoordinator[HyCubeData]):
    """Polls /get_values/ often, /data_row/ rarely, integrates energy."""

    config_entry: HyCubeConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: HyCubeConfigEntry, api: HyCubeApi
    ) -> None:
        self._base_interval = timedelta(
            seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        )
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=self._base_interval,
            always_update=True,
        )
        self.api = api
        self.info: dict[str, Any] = {}
        self.energy = EnergyAccumulator()
        self.profile = ConsumptionProfile()
        self.mode: str = MODE_STANDARD
        self.target_soc: int = DEFAULT_TARGET_SOC
        self.last_backfill: dict[str, Any] = {}
        self.capacity = CapacityEstimator()
        # Energy the battery takes per 100 % SoC; with `capacity` the efficiency.
        self.charge_capacity = CapacityEstimator(rising=True)
        self.planner = HyCubePlanner(hass, self)
        self.forecaster = ConsumptionForecaster(hass, self)
        # Hourly consumption and PV production of all past days, kept for good.
        self.archive = Archive()
        self._rebuild_lock = asyncio.Lock()
        # Highest battery power held for a minute, W.
        self.peak_charge: float | None = None
        self.peak_discharge: float | None = None
        self._recent_power: deque[tuple[float, float]] = deque()
        self._status_interval = entry.options.get(
            CONF_STATUS_INTERVAL, DEFAULT_STATUS_INTERVAL
        )
        self._last_status = 0.0
        self._last_success = 0.0
        self._failures = 0
        self._backfill_attempts = 0
        self._mode_set_at = 0.0
        self.hold_active: int | None = None  # x_active last sent for "hold"
        # Newest /get_values/ response; self.data is only replaced after the
        # update returns, so calculations during an update must not use it.
        self._latest_values: dict[str, Any] = {}
        self._hold_grid_polls = 0
        # Leaving "hold": watch until this time whether the battery resumes.
        self._release_until = 0.0
        self._release_idle_since: float | None = None
        # When "hold" was left, until the battery is seen discharging again.
        self._hold_left_at: float | None = None
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        # The archive grows large and changes once a day, so it has a store of
        # its own instead of being rewritten with the totals every 30 s.
        self._archive_store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, archive_store_key(entry.entry_id)
        )

    # --- lifecycle --------------------------------------------------------------
    async def _async_setup(self) -> None:
        stored = await self._store.async_load() or {}
        self.energy = EnergyAccumulator(stored.get("totals"))
        if (last := stored.get("last")) and isinstance(last, list) and len(last) == 2:
            self.energy.last = (float(last[0]), {k: float(v) for k, v in last[1].items()})
        self.mode = stored.get("mode", MODE_STANDARD)
        if self.mode not in MODES:
            self.mode = MODE_STANDARD
        self.target_soc = int(stored.get("target_soc", DEFAULT_TARGET_SOC))
        self.hold_active = stored.get("hold_active")
        self.profile = ConsumptionProfile.from_dict(stored.get("profile"))
        self.forecaster.restore(stored.get("forecaster"))
        self.peak_charge = to_float(stored.get("peak_charge"))
        self.peak_discharge = to_float(stored.get("peak_discharge"))
        if (archived := await self._archive_store.async_load()) is not None:
            self.archive = Archive(archived)
        else:
            # Versions before 1.2 kept the last weeks in the main store.
            self.archive = Archive.from_legacy(stored.get("history"))
        self.last_backfill = stored.get("last_backfill") or {}
        self.capacity = CapacityEstimator(stored.get("capacity"))
        self.charge_capacity = CapacityEstimator(
            stored.get("charge_capacity"), rising=True
        )
        self.planner.restore(stored.get("planner"))
        try:
            self.info = await self.api.get_info()
        except HyCubeAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except HyCubeError as err:
            raise UpdateFailed(f"HyCube not reachable: {err}") from err

    @callback
    def start_profile_schedule(self) -> None:
        """Rebuild the profile daily and shortly after start; start planning."""
        self.planner.start()
        self.forecaster.start()
        self.config_entry.async_on_unload(
            async_track_time_change(
                self.hass, self._scheduled_profile_rebuild, hour=0, minute=10, second=0
            )
        )
        self.config_entry.async_create_background_task(
            self.hass, self.async_rebuild_profile(), f"{DOMAIN}_profile_initial"
        )

    async def _scheduled_profile_rebuild(self, _now: datetime) -> None:
        await self.async_rebuild_profile()

    async def async_shutdown(self) -> None:
        """Persist totals immediately on unload/stop."""
        await self._store.async_save(self._store_data())
        await self.async_save_archive()
        await super().async_shutdown()

    @callback
    def _store_data(self) -> dict[str, Any]:
        last = self.energy.last
        return {
            "totals": self.energy.totals,
            "last": [last[0], last[1]] if last else None,
            "mode": self.mode,
            "target_soc": self.target_soc,
            "hold_active": self.hold_active,
            "profile": self.profile.as_dict(),
            "forecaster": self.forecaster.as_dict(),
            "peak_charge": self.peak_charge,
            "peak_discharge": self.peak_discharge,
            "last_backfill": self.last_backfill,
            "capacity": self.capacity.as_dict(),
            "charge_capacity": self.charge_capacity.as_dict(),
            "planner": self.planner.as_dict(),
        }

    @callback
    def _schedule_save(self) -> None:
        self._store.async_delay_save(self._store_data, STORAGE_SAVE_DELAY)

    @callback
    def async_schedule_save(self) -> None:
        """Persist the stored state soon (for the planner and entities)."""
        self._schedule_save()

    async def async_save_archive(self) -> None:
        """Write the archive if it changed."""
        if self.archive.dirty:
            self.archive.dirty = False
            await self._archive_store.async_save(self.archive.as_dict())

    # --- polling ----------------------------------------------------------------
    async def _async_update_data(self) -> HyCubeData:
        try:
            values = await self.api.get_values()
            self._latest_values = values
            now = time.time()
            live = powers_from_values(values)
            gap = await self._handle_gap(now, live)
            if gap == "retry":
                # Backfill will be retried; keep the series open until then.
                raise HyCubeError("gap backfill pending")
            if gap == "none":
                self.energy.add_series(
                    [(now, live)], max_gap=GAP_MAX_INTERPOLATE.total_seconds()
                )
        except HyCubeAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except HyCubeError as err:
            return self._handle_failure(err)

        status = self.data.status if self.data else {}
        if now - self._last_status >= self._status_interval:
            try:
                status = await self.api.get_data_row()
            except HyCubeAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except HyCubeError as err:
                # The values above are fresh; the flags are retried in 30 s.
                _LOGGER.debug("HyCube status flags not read: %s", err)
                self._last_status = now - max(self._status_interval - 30, 0)
            else:
                self._last_status = now
                self._sync_mode_from_status(status)

        if self.mode == MODE_HOLD:
            self._check_hold(values)
        elif self.mode == MODE_STANDARD and now < self._release_until:
            self._check_release(values, now)
        self._track_peaks(now, to_float(values.get("Battery_P")))
        if self._hold_left_at is not None and (
            (to_float(values.get("Battery_P")) or 0.0) < -HOLD_RELEASE_GRID_MIN
            or now - self._hold_left_at >= HOLD_RELEASE_IDLE
        ):
            self._hold_left_at = None  # discharging again, or waited long enough
        if (soc := to_float(values.get("Battery_C"))) is not None:
            totals = self.energy.totals
            discharged = totals[ENERGY_BATTERY_DISCHARGE]
            charged = totals[ENERGY_BATTERY_CHARGE]
            self.capacity.update(soc, discharged, charged)
            self.charge_capacity.update(soc, charged, discharged)

        if self._failures:
            _LOGGER.info("HyCube reachable again after %s failed polls", self._failures)
        self._failures = 0
        self._last_success = now
        self.update_interval = self._base_interval
        self._schedule_save()
        return HyCubeData(values=values, status=status, sample_time=dt_util.utcnow())

    def _handle_failure(self, err: HyCubeError) -> HyCubeData:
        """Back off; keep showing the last values for short hiccups."""
        self._failures += 1
        backoff = self._base_interval * 2 ** min(self._failures, 6)
        if isinstance(err, HyCubeBusyError):
            _LOGGER.warning("HyCube reports too many connections, backing off")
            backoff = max(backoff, BUSY_BACKOFF * 2 ** min(self._failures - 1, 3))
        self.update_interval = min(backoff, MAX_BACKOFF)
        if (
            self.data is not None
            and time.time() - self._last_success < STALE_TOLERANCE.total_seconds()
        ):
            _LOGGER.debug("HyCube poll failed, keeping last values: %s", err)
            return HyCubeData(
                values=self.data.values,
                status=self.data.status,
                sample_time=self.data.sample_time,
                stale=True,
            )
        raise UpdateFailed(f"HyCube poll failed: {err}") from err

    async def _handle_gap(self, now: float, live: dict[str, float]) -> str:
        """Fill a hole in the sample series from the device's 5-minute statistics.

        Returns "none" (no gap), "filled" (energy.last moved to now) or
        "retry" (transient error, try again on the next poll).
        """
        last = self.energy.last
        if last is None:
            return "none"
        t0 = last[0]
        gap = now - t0
        threshold = max(
            self._base_interval.total_seconds() * GAP_BACKFILL_FACTOR,
            GAP_MIN_BACKFILL.total_seconds(),
        )
        if gap <= threshold:
            return "none"

        tz = dt_util.get_default_time_zone()
        first_day = max(
            dt_util.as_local(dt_util.utc_from_timestamp(t0)).date(),
            dt_util.now().date() - timedelta(days=BACKFILL_MAX_DAYS - 1),
        )
        buckets = []
        day = first_day
        try:
            while day <= dt_util.now().date():
                buckets += buckets_from_day_stats(await self.api.get_day_stats(day), day, tz)
                day += timedelta(days=1)
        except HyCubeAuthError:
            raise
        except HyCubeError as err:
            self._backfill_attempts += 1
            if self._backfill_attempts < BACKFILL_RETRIES:
                _LOGGER.debug("HyCube gap backfill failed, will retry: %s", err)
                return "retry"
            _LOGGER.warning("HyCube gap backfill failed, giving up: %s", err)
            self._backfill_attempts = 0
            buckets = []
        self._backfill_attempts = 0

        before = dict(self.energy.totals)
        covered = self.energy.add_buckets(buckets, t0, now)
        covered_until = max(
            (end for start, end, _ in buckets if start < now and end > t0), default=t0
        )
        covered_until = min(covered_until, now)
        tail = now - covered_until
        # The newest 5-minute bucket is not written yet: bridge the rest with
        # the live power if that stretch is short enough to trust.
        if covered and tail <= GAP_MAX_INTERPOLATE.total_seconds():
            self.energy.integrate((covered_until, live), (now, live))
            covered += tail
        elif not covered and gap <= GAP_MAX_INTERPOLATE.total_seconds():
            self.energy.integrate(last, (now, live))
            covered = gap
        self.energy.last = (now, live)

        self.last_backfill = {
            "time": dt_util.utcnow().isoformat(),
            "gap_seconds": round(gap),
            "buckets": sum(1 for s, e, _ in buckets if e > t0 and s < now),
            "added_kwh": {
                k: round((self.energy.totals[k] - before[k]) / 1000, 3) for k in before
            },
            "unrecoverable_seconds": round(max(gap - covered, 0)),
        }
        _LOGGER.info(
            "HyCube gap of %ss filled from device statistics (unrecoverable: %ss)",
            round(gap),
            self.last_backfill["unrecoverable_seconds"],
        )
        return "filled"

    @callback
    def _sync_mode_from_status(self, status: dict[str, Any]) -> None:
        """Keep the mode select in line with what the device reports."""
        manual = to_float(status.get("manualChargingActivation"))
        soc = to_float(status.get("manualChargingSoc"))
        if soc is not None:
            self.target_soc = int(soc)
        if manual == 1:
            self.mode = MODE_GRID_CHARGE
        elif (
            manual == 0
            and self.mode == MODE_GRID_CHARGE
            and time.time() - self._mode_set_at > MODE_READBACK_GRACE
        ):
            # Finished or switched off on the device itself.
            self.mode = MODE_STANDARD

    # --- battery model --------------------------------------------------------------
    @property
    def soc(self) -> float | None:
        """State of charge of the newest sample, %."""
        return to_float(self._latest_values.get("Battery_C"))

    def usable_capacity(self) -> float:
        """kWh per 100 % SoC: measured once there are samples, else configured."""
        if self.capacity.kwh and self.capacity.samples >= EFFICIENCY_MIN_SAMPLES:
            return self.capacity.kwh
        return float(
            self.config_entry.options.get(CONF_BATTERY_CAPACITY, DEFAULT_BATTERY_CAPACITY)
        )

    def _track_peaks(self, now: float, power: float | None) -> None:
        """Remember the highest charge and discharge power held for a minute."""
        window = self._recent_power
        if power is None:
            window.clear()
            return
        window.append((now, power))
        while now - window[0][0] > PEAK_WINDOW:
            window.popleft()
        if now - window[0][0] < PEAK_WINDOW * 0.8:
            return  # not a full minute of samples yet
        lowest = min(p for _, p in window)
        highest = max(p for _, p in window)
        if lowest > (self.peak_charge or 0.0):
            self.peak_charge = lowest
        if -highest > (self.peak_discharge or 0.0):
            self.peak_discharge = -highest

    def charge_power(self) -> float | None:
        """W the battery was seen charging with at most, if that is known."""
        peak = self.peak_charge
        return peak if peak is not None and peak >= PEAK_MIN_POWER else None

    def discharge_power(self) -> float | None:
        """W the battery was seen delivering at most, if that is known."""
        peak = self.peak_discharge
        return peak if peak is not None and peak >= PEAK_MIN_POWER else None

    def release_idle_seconds(self) -> float:
        """How long the battery may still not discharge after "hold" was left."""
        if self._hold_left_at is None:
            return 0.0
        return max(self._hold_left_at + HOLD_RELEASE_IDLE - time.time(), 0.0)

    def efficiency(self) -> float | None:
        """Measured share of the charged energy the battery gives back."""
        return round_trip_efficiency(
            self.capacity, self.charge_capacity, EFFICIENCY_MIN_SAMPLES
        )

    # --- control ------------------------------------------------------------------
    def _current_soc(self) -> int | None:
        soc = to_float(self._latest_values.get("Battery_C"))
        return None if soc is None else round(soc)

    @staticmethod
    def hold_active_for(soc: int) -> int:
        """Normal-operation share that puts the reserve limit at `soc`."""
        return max(HOLD_MIN_ACTIVE, min(100, 100 - soc))

    def discharge_floor(self) -> int:
        """SoC below which the battery does not supply the home in normal use.

        The device does not report its battery split, so the x_active of the
        mode's command template is used. "Hold" raises the reserve only for the
        time being; the forecast keeps the split of "standard" so it shows how
        long the held energy lasts once released. Templates without a split
        request leave it unchanged, so "standard" and then the default apply.
        """
        modes = [MODE_STANDARD] if self.mode == MODE_HOLD else [self.mode, MODE_STANDARD]
        active = BATTERY_ACTIVE_DEFAULT
        for mode in modes:
            if match := _SPLIT_ACTIVE.search(self.command_for(mode)):
                active = int(match.group(1))
                break
        return max(BATTERY_PROTECTION, min(100, 100 - active))

    def render_commands(self, mode: str) -> list[str]:
        """Requests for a mode with placeholders filled, one per line."""
        template = self.command_for(mode)
        if not template:
            return []
        soc = self._current_soc()
        if soc is None and ("{current_soc}" in template or "{hold_active}" in template):
            raise HomeAssistantError("Current state of charge unknown, try again shortly.")
        values = {"{soc}": str(self.target_soc)}
        if soc is not None:
            values["{current_soc}"] = str(soc)
            values["{hold_active}"] = str(self.hold_active_for(soc))
        commands = []
        for line in template.replace(";", "\n").splitlines():
            if not (line := line.strip()):
                continue
            for key, val in values.items():
                line = line.replace(key, val)
            commands.append(line)
        return commands

    def _check_hold(self, values: dict[str, Any]) -> None:
        """Follow PV charging upwards and watch for grid charging while holding."""
        battery = to_float(values.get("Battery_P")) or 0.0
        solar = to_float(values.get("solar_total_P")) or 0.0
        if battery > HOLD_GRID_CHARGE_WARN and solar < HOLD_PV_IDLE:
            self._hold_grid_polls += 1
            if self._hold_grid_polls == 3:
                _LOGGER.warning(
                    "HyCube charges the battery with %.0f W without PV while in mode "
                    "'hold' - the controller seems to refill the reserve from the grid",
                    battery,
                )
                self.planner.block_hold()
        else:
            self._hold_grid_polls = 0

        soc = self._current_soc()
        if soc is None or self.hold_active is None or "{hold_active}" not in self.command_for(MODE_HOLD):
            return
        target = self.hold_active_for(soc)
        if (
            target <= self.hold_active - HOLD_RESEND_STEP
            and time.time() - self._mode_set_at >= HOLD_RESEND_MIN_INTERVAL
        ):
            _LOGGER.debug("HyCube hold limit follows SoC %s%%", soc)
            self.config_entry.async_create_background_task(
                self.hass, self._resend_hold(), f"{DOMAIN}_hold_update"
            )

    def _check_release(self, values: dict[str, Any], now: float) -> None:
        """Warn once if the battery keeps idling after "hold" was left."""
        battery = to_float(values.get("Battery_P")) or 0.0
        grid = to_float(values.get("Grid_P")) or 0.0
        soc = to_float(values.get("Battery_C"))
        if (
            battery < -HOLD_RELEASE_GRID_MIN
            or soc is None
            or soc <= self.discharge_floor() + 2
        ):
            # Discharging again, or nothing left to give: nothing to report.
            self._release_until = 0.0
            return
        if grid < HOLD_RELEASE_GRID_MIN:
            self._release_idle_since = None
            return
        if self._release_idle_since is None:
            self._release_idle_since = now
        elif now - self._release_idle_since >= HOLD_RELEASE_WARN_AFTER:
            _LOGGER.warning(
                "HyCube does not discharge %.0f min after leaving 'hold': SoC %.0f%% is "
                "above the reserve (%s%%) but the home draws %.0f W from the grid",
                (now - self._release_idle_since) / 60,
                soc,
                self.discharge_floor(),
                grid,
            )
            self._release_until = 0.0

    async def _resend_hold(self) -> None:
        try:
            await self.async_set_mode(MODE_HOLD)
        except HomeAssistantError as err:
            _LOGGER.warning("HyCube hold update failed: %s", err)

    def command_for(self, mode: str) -> str:
        """Configured request template for a mode ('' if none)."""
        opts = self.config_entry.options
        key = MODE_COMMAND_OPTION[mode]
        value = opts[key] if key in opts else MODE_COMMAND_DEFAULT[mode]
        return (value or "").strip()

    async def async_set_mode(self, mode: str) -> None:
        """Send the configured command for a battery mode."""
        if mode not in MODES:
            raise HomeAssistantError(f"Unknown mode {mode}")
        if not self.config_entry.options.get(CONF_CONTROL_ENABLED, True):
            raise HomeAssistantError(
                "Battery control is disabled. Enable it in the HyCube integration options."
            )
        commands = self.render_commands(mode)
        if not commands:
            raise HomeAssistantError(
                f"No command configured for mode '{mode}' in the HyCube options."
            )
        for command in commands:
            try:
                await self.api.send_command(command)
            except HyCubeError as err:
                raise HomeAssistantError(
                    f"HyCube command {command} failed: {err}"
                ) from err
        if self.mode == MODE_HOLD and mode != MODE_HOLD:
            self._release_until = time.time() + HOLD_RELEASE_WATCH
            self._release_idle_since = None
            self._hold_left_at = time.time()
        elif mode == MODE_HOLD:
            self._hold_left_at = None
        if mode == MODE_HOLD and (soc := self._current_soc()) is not None:
            self.hold_active = self.hold_active_for(soc)
        elif mode != MODE_HOLD:
            self.hold_active = None
        self._hold_grid_polls = 0
        self.mode = mode
        self._mode_set_at = time.time()
        self._last_status = 0  # read back the flags on the next poll
        self._schedule_save()
        self.async_update_listeners()
        await self.async_request_refresh()

    async def async_set_target_soc(self, soc: int) -> None:
        """Change the grid-charge target; re-sends the command while active."""
        self.target_soc = soc
        self._schedule_save()
        if self.mode == MODE_GRID_CHARGE and "{soc}" in self.command_for(MODE_GRID_CHARGE):
            await self.async_set_mode(MODE_GRID_CHARGE)
        else:
            self.async_update_listeners()

    # --- consumption profile -------------------------------------------------------
    def profile_source(self) -> str:
        """Where the profile comes from: an entity id or 'device'."""
        return self.config_entry.options.get(CONF_PROFILE_SOURCE) or "device"

    async def _sync_device_history(self) -> None:
        """Add past days from /db_today/ to the archive.

        Past days never change, so each day is fetched once. Missing recent
        days come first, then the archive grows further into the past, a few
        days per run with pauses in between to keep the load on the
        controller low, until the device has nothing older.
        """
        tz = dt_util.get_default_time_zone()
        fetched = 0
        for day in self.archive.pending(dt_util.now().date(), ARCHIVE_DAYS_PER_RUN):
            if fetched:
                await asyncio.sleep(HISTORY_REQUEST_SPACING)
            try:
                stats = await self.api.get_day_stats(day)
            except HyCubeError as err:
                _LOGGER.debug("HyCube history for %s not loaded: %s", day, err)
                break  # try the rest on the next rebuild
            fetched += 1
            buckets = buckets_from_day_stats(stats, day, tz)
            self.archive.set_energy(
                day,
                _day_hours(buckets, ENERGY_HOME, day, tz),
                _day_hours(buckets, ENERGY_SOLAR, day, tz),
                _day_hours(buckets, ENERGY_GRID_IMPORT, day, tz),
                _day_hours(buckets, ENERGY_GRID_EXPORT, day, tz),
            )
        if fetched:
            self.archive.check_complete()
            _LOGGER.debug("HyCube added %s days to the archive", fetched)
            await self.async_save_archive()

    async def _recorder_rows(self, stat_id: str) -> list[tuple[datetime, float]]:
        end = dt_util.start_of_local_day()
        start = end - timedelta(days=RECORDER_YEARS * 366)
        stats = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            dt_util.as_utc(start),
            dt_util.as_utc(end),
            {stat_id},
            "hour",
            {"energy": UnitOfEnergy.KILO_WATT_HOUR},
            {"change"},
        )
        rows = []
        for row in stats.get(stat_id, []):
            begin = row.get("start")
            change = row.get("change")
            if begin is None or change is None:
                continue
            if isinstance(begin, (int, float)):
                begin = dt_util.utc_from_timestamp(begin)
            rows.append((dt_util.as_local(begin), float(change)))
        return rows

    async def async_rebuild_profile(self) -> None:
        """Bring the archive up to date and learn the consumption from it."""
        async with self._rebuild_lock:
            source = self.profile_source()
            # The archive is filled in any case: it also holds the PV production.
            await self._sync_device_history()
            if source == "device":
                rows = self.archive.rows(dt_util.get_default_time_zone())
                if not rows:
                    # Device keeps no history: fall back to this integration's sensor.
                    own = er.async_get(self.hass).async_get_entity_id(
                        "sensor", DOMAIN, f"{self.config_entry.unique_id}_energy_{ENERGY_HOME}"
                    )
                    rows = await self._recorder_rows(own) if own else []
            else:
                rows = await self._recorder_rows(source)
            profile = ConsumptionProfile.build(rows, dt_util.now())
            profile.hours = self.profile.hours  # stays until the forecast is refreshed
            self.profile = profile
            _LOGGER.debug("HyCube profile rebuilt from %s (%s hours)", source, len(rows))
            self._schedule_save()
            self.async_update_listeners()
            await self.forecaster.async_rebuild(rows)


def archive_store_key(entry_id: str) -> str:
    """Name of the archive's store file."""
    return f"{DOMAIN}.{entry_id}.history"


def _day_hours(
    buckets: list[Any], key: str, day: date, tz: Any
) -> list[float | None] | None:
    """24 hourly kWh of one flow on one day; None without data."""
    by_hour = {
        start.hour: kwh
        for start, kwh in hourly_kwh(buckets, key, tz).items()
        if start.date() == day
    }
    return [by_hour.get(h) for h in range(24)] if by_hour else None
