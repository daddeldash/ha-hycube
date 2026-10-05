"""Data update coordinator for HyCube."""

from __future__ import annotations

import asyncio
import logging
import re
import time
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
from .const import (
    BATTERY_ACTIVE_DEFAULT,
    BATTERY_PROTECTION,
    BUSY_BACKOFF,
    CONF_CONTROL_ENABLED,
    CONF_PROFILE_SOURCE,
    CONF_PROFILE_WEEKS,
    CONF_SCAN_INTERVAL,
    CONF_STATUS_INTERVAL,
    DEFAULT_PROFILE_WEEKS,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_STATUS_INTERVAL,
    DEFAULT_TARGET_SOC,
    DOMAIN,
    ENERGY_HOME,
    GAP_BACKFILL_FACTOR,
    GAP_MAX_INTERPOLATE,
    GAP_MIN_BACKFILL,
    HISTORY_REQUEST_SPACING,
    HOLD_GRID_CHARGE_WARN,
    HOLD_MIN_ACTIVE,
    HOLD_PV_IDLE,
    HOLD_RESEND_MIN_INTERVAL,
    HOLD_RESEND_STEP,
    MAX_BACKOFF,
    MODE_COMMAND_DEFAULT,
    MODE_COMMAND_OPTION,
    MODE_GRID_CHARGE,
    MODE_HOLD,
    MODE_STANDARD,
    MODES,
    STALE_TOLERANCE,
    STORAGE_SAVE_DELAY,
    STORAGE_VERSION,
)
from .energy import (
    EnergyAccumulator,
    buckets_from_day_stats,
    hourly_kwh,
    powers_from_values,
    to_float,
)
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
        # Hourly household kWh per past day from the device's own statistics.
        self.history: dict[str, list[float | None] | None] = {}
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
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
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
        self.history = stored.get("history") or {}
        try:
            self.info = await self.api.get_info()
        except HyCubeAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except HyCubeError as err:
            raise UpdateFailed(f"HyCube not reachable: {err}") from err

    @callback
    def start_profile_schedule(self) -> None:
        """Rebuild the profile daily and shortly after start."""
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
            "history": self.history,
        }

    @callback
    def _schedule_save(self) -> None:
        self._store.async_delay_save(self._store_data, STORAGE_SAVE_DELAY)

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
            status = self.data.status if self.data else {}
            if now - self._last_status >= self._status_interval:
                status = await self.api.get_data_row()
                self._last_status = now
                self._sync_mode_from_status(status)
        except HyCubeAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except HyCubeError as err:
            return self._handle_failure(err)

        if self.mode == MODE_HOLD:
            self._check_hold(values)

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

    def _weeks(self) -> int:
        return int(self.config_entry.options.get(CONF_PROFILE_WEEKS, DEFAULT_PROFILE_WEEKS))

    async def _sync_device_history(self) -> None:
        """Fetch hourly household consumption of past days from /db_today/.

        Past days never change, so each day is fetched once and cached in the
        store. Requests are spaced out to keep the load on the controller low.
        """
        tz = dt_util.get_default_time_zone()
        today = dt_util.now().date()
        wanted = [today - timedelta(days=d) for d in range(1, self._weeks() * 7 + 1)]
        self.history = {k: v for k, v in self.history.items() if date.fromisoformat(k) in wanted}
        fetched = 0
        for day in wanted:
            if day.isoformat() in self.history:
                continue
            if fetched:
                await asyncio.sleep(HISTORY_REQUEST_SPACING)
            try:
                stats = await self.api.get_day_stats(day)
            except HyCubeError as err:
                _LOGGER.debug("HyCube history for %s not loaded: %s", day, err)
                break  # try the rest on the next rebuild
            fetched += 1
            hours = hourly_kwh(buckets_from_day_stats(stats, day, tz), ENERGY_HOME, tz)
            if not hours:
                self.history[day.isoformat()] = None  # no data on the device
                continue
            by_hour = {start.hour: kwh for start, kwh in hours.items()}
            self.history[day.isoformat()] = [
                round(by_hour[h], 4) if h in by_hour else None for h in range(24)
            ]
        if fetched:
            _LOGGER.debug("HyCube loaded %s days of device history", fetched)
            self._schedule_save()

    def _device_history_rows(self) -> list[tuple[datetime, float]]:
        tz = dt_util.get_default_time_zone()
        rows = []
        for day_iso, hours in self.history.items():
            if not hours:
                continue
            start = datetime.combine(date.fromisoformat(day_iso), datetime.min.time(), tzinfo=tz)
            rows += [
                (start + timedelta(hours=h), kwh)
                for h, kwh in enumerate(hours)
                if kwh is not None
            ]
        return rows

    async def _recorder_rows(self, stat_id: str) -> list[tuple[datetime, float]]:
        end = dt_util.start_of_local_day()
        start = end - timedelta(weeks=self._weeks())
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
        """Rebuild the profile from device history or recorder statistics."""
        source = self.profile_source()
        if source == "device":
            await self._sync_device_history()
            rows = self._device_history_rows()
            if not rows:
                # Device keeps no history: fall back to this integration's sensor.
                own = er.async_get(self.hass).async_get_entity_id(
                    "sensor", DOMAIN, f"{self.config_entry.unique_id}_energy_{ENERGY_HOME}"
                )
                rows = await self._recorder_rows(own) if own else []
        else:
            rows = await self._recorder_rows(source)
        self.profile = ConsumptionProfile.build(rows, dt_util.now())
        _LOGGER.debug("HyCube profile rebuilt from %s (%s hours)", source, len(rows))
        self._schedule_save()
        self.async_update_listeners()
