"""Constants for the HyCube integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "hycube"
MANUFACTURER: Final = "HyCube Technologies GmbH"

DEFAULT_USERNAME: Final = "hycube"
DEFAULT_PASSWORD: Final = "hycube"

# --- Options -----------------------------------------------------------------
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_STATUS_INTERVAL: Final = "status_interval"
CONF_BATTERY_CAPACITY: Final = "battery_capacity"
CONF_PROFILE_WEEKS: Final = "profile_weeks"
CONF_PROFILE_SOURCE: Final = "profile_source"
CONF_CONTROL_ENABLED: Final = "control_enabled"
CONF_CMD_STANDARD: Final = "cmd_standard"
CONF_CMD_HOLD: Final = "cmd_hold"
CONF_CMD_GRID_CHARGE: Final = "cmd_grid_charge"

DEFAULT_SCAN_INTERVAL: Final = 10  # s, realtime power values (/get_values/)
MIN_SCAN_INTERVAL: Final = 5
DEFAULT_STATUS_INTERVAL: Final = 60  # s, status flags (/data_row/)
DEFAULT_BATTERY_CAPACITY: Final = 10.0  # kWh usable
DEFAULT_PROFILE_WEEKS: Final = 8

# Command templates for the battery modes, one request per line, sent in order.
# From the HyWeb UI (2.065):
#   /smartCharging/ManualChargingActivation/?value=<SoC>  grid charging up to SoC
#   /smartCharging/ManualChargingActivation/?value=       stop grid charging
#   /Bat/setCustomBat/?x_active=<%>&x_passive=<%>         battery split: normal
#       operation and buffer; emergency reserve is the rest above 5 % protection
# There is no "block discharge" request. "Hold" shrinks normal operation so the
# reserve starts at the current SoC (x_active = 100 - SoC, at least 10).
# Placeholders: {soc} grid-charge target, {current_soc}, {hold_active}.
BATTERY_PROTECTION: Final = 5  # % deep-discharge protection, never usable
BATTERY_ACTIVE_DEFAULT: Final = 85  # % normal operation of the default split
BATTERY_SPLIT_DEFAULT: Final = (
    f"/Bat/setCustomBat/?x_active={BATTERY_ACTIVE_DEFAULT}&x_passive=5"
)
CHARGE_STOP: Final = "/smartCharging/ManualChargingActivation/?value="
DEFAULT_CMD_STANDARD: Final = f"{CHARGE_STOP}\n{BATTERY_SPLIT_DEFAULT}"
DEFAULT_CMD_HOLD: Final = (
    f"{CHARGE_STOP}\n/Bat/setCustomBat/?x_active={{hold_active}}&x_passive=5"
)
DEFAULT_CMD_GRID_CHARGE: Final = (
    f"{BATTERY_SPLIT_DEFAULT}\n/smartCharging/ManualChargingActivation/?value={{soc}}"
)
# Single-request defaults of entry version 1.1, replaced by the migration.
LEGACY_CMD_STANDARD: Final = CHARGE_STOP
LEGACY_CMD_GRID_CHARGE: Final = "/smartCharging/ManualChargingActivation/?value={soc}"

HOLD_MIN_ACTIVE: Final = 10  # % normal operation never set below this
HOLD_RESEND_STEP: Final = 5  # % SoC rise before the hold limit is moved up
HOLD_RESEND_MIN_INTERVAL: Final = 900  # s between two hold updates
# Battery charging above this while PV is below HOLD_PV_IDLE in hold mode
# means the controller refills the reserve from the grid.
HOLD_GRID_CHARGE_WARN: Final = 100  # W
HOLD_PV_IDLE: Final = 50  # W

# --- Polling behaviour --------------------------------------------------------
MAX_BACKOFF: Final = timedelta(minutes=5)
# After "Too many connections" stay away at least this long.
BUSY_BACKOFF: Final = timedelta(seconds=60)
TOKEN_REFRESH_MARGIN: Final = 120  # s before "exp" a new token is requested
REQUEST_TIMEOUT: Final = 10  # s
REQUEST_SPACING: Final = 0.5  # s minimum between two requests to the device
# Transient poll failures within this window keep the last values instead of
# turning every entity unavailable.
STALE_TOLERANCE: Final = timedelta(seconds=120)
# Past days of /db_today/ fetched for the consumption profile, one at a time.
HISTORY_REQUEST_SPACING: Final = 3.0  # s

# A gap between two successful samples longer than this triggers a backfill
# from the 5-minute statistics of /db_today/.
GAP_BACKFILL_FACTOR: Final = 3
GAP_MIN_BACKFILL: Final = timedelta(seconds=90)
# Without backfill data, gaps up to this length are bridged by trapezoid rule;
# longer gaps are skipped rather than guessed.
GAP_MAX_INTERPOLATE: Final = timedelta(minutes=10)

STORAGE_VERSION: Final = 1
STORAGE_SAVE_DELAY: Final = 30  # s

# --- Battery modes ------------------------------------------------------------
MODE_STANDARD: Final = "standard"
MODE_HOLD: Final = "hold"
MODE_GRID_CHARGE: Final = "grid_charge"
MODES: Final = [MODE_STANDARD, MODE_HOLD, MODE_GRID_CHARGE]
MODE_COMMAND_OPTION: Final = {
    MODE_STANDARD: CONF_CMD_STANDARD,
    MODE_HOLD: CONF_CMD_HOLD,
    MODE_GRID_CHARGE: CONF_CMD_GRID_CHARGE,
}
MODE_COMMAND_DEFAULT: Final = {
    MODE_STANDARD: DEFAULT_CMD_STANDARD,
    MODE_HOLD: DEFAULT_CMD_HOLD,
    MODE_GRID_CHARGE: DEFAULT_CMD_GRID_CHARGE,
}

DEFAULT_TARGET_SOC: Final = 80

# --- Energy counters (keys of the persisted totals, Wh) -----------------------
ENERGY_GRID_IMPORT: Final = "grid_import"
ENERGY_GRID_EXPORT: Final = "grid_export"
ENERGY_BATTERY_CHARGE: Final = "battery_charge"
ENERGY_BATTERY_DISCHARGE: Final = "battery_discharge"
ENERGY_SOLAR: Final = "solar"
ENERGY_EXTERNAL: Final = "external_production"
ENERGY_HOME: Final = "home"
ENERGY_KEYS: Final = [
    ENERGY_GRID_IMPORT,
    ENERGY_GRID_EXPORT,
    ENERGY_BATTERY_CHARGE,
    ENERGY_BATTERY_DISCHARGE,
    ENERGY_SOLAR,
    ENERGY_EXTERNAL,
    ENERGY_HOME,
]
