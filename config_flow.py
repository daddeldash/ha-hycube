"""Config and options flow for HyCube."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import HyCubeApi, HyCubeAuthError, HyCubeError
from .const import (
    CONF_BATTERY_CAPACITY,
    CONF_CMD_GRID_CHARGE,
    CONF_CMD_HOLD,
    CONF_CMD_STANDARD,
    CONF_CONTROL_ENABLED,
    CONF_EFFICIENCY,
    CONF_EXPORT_PRICE,
    CONF_GRID_CHARGE_POWER,
    CONF_MAX_CHARGE_SOC,
    CONF_PLAN_GRID_CHARGE,
    CONF_PLAN_HOLD,
    CONF_PROFILE_SOURCE,
    CONF_PROFILE_WEEKS,
    CONF_PV_AUTO_CORRECT,
    CONF_PV_FACTOR,
    CONF_SCAN_INTERVAL,
    CONF_SHIFT_COST,
    CONF_STANDBY_POWER,
    CONF_STATUS_INTERVAL,
    CONF_TEMPERATURE_ENTITY,
    CONF_TIBBER_HOME,
    CONF_WEATHER_ENTITY,
    DEFAULT_BATTERY_CAPACITY,
    DEFAULT_CMD_GRID_CHARGE,
    DEFAULT_CMD_HOLD,
    DEFAULT_CMD_STANDARD,
    DEFAULT_EXPORT_PRICE,
    DEFAULT_GRID_CHARGE_POWER,
    DEFAULT_MAX_CHARGE_SOC,
    DEFAULT_PASSWORD,
    DEFAULT_PV_FACTOR,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SHIFT_COST,
    DEFAULT_STANDBY_POWER,
    DEFAULT_STATUS_INTERVAL,
    DEFAULT_USERNAME,
    DOMAIN,
    MIN_SCAN_INTERVAL,
)
from .planner import async_tibber_prices

# Options of the two steps; saving one step keeps the other one's values.
DEVICE_OPTIONS = (
    CONF_SCAN_INTERVAL,
    CONF_STATUS_INTERVAL,
    CONF_BATTERY_CAPACITY,
    CONF_PROFILE_WEEKS,  # removed from the form; saving the step drops the old value
    CONF_PROFILE_SOURCE,
    CONF_TEMPERATURE_ENTITY,
    CONF_WEATHER_ENTITY,
    CONF_CONTROL_ENABLED,
    CONF_CMD_STANDARD,
    CONF_CMD_HOLD,
    CONF_CMD_GRID_CHARGE,
)
SCHEDULE_OPTIONS = (
    CONF_TIBBER_HOME,
    CONF_PLAN_GRID_CHARGE,
    CONF_PLAN_HOLD,
    CONF_GRID_CHARGE_POWER,
    CONF_MAX_CHARGE_SOC,
    CONF_EFFICIENCY,
    CONF_SHIFT_COST,
    CONF_EXPORT_PRICE,
    CONF_PV_FACTOR,
    CONF_PV_AUTO_CORRECT,
    CONF_STANDBY_POWER,
)


class HyCubeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up a HyCube by IP address."""

    VERSION = 1
    MINOR_VERSION = 2

    async def _validate(self, data: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        api = HyCubeApi(
            async_get_clientsession(self.hass),
            data[CONF_HOST],
            data[CONF_USERNAME],
            data[CONF_PASSWORD],
        )
        try:
            return await api.get_info(), None
        except HyCubeAuthError:
            return None, "invalid_auth"
        except HyCubeError:
            return None, "cannot_connect"

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            info, error = await self._validate(user_input)
            if error:
                errors["base"] = error
            else:
                serial = _serial(info) or user_input[CONF_HOST]
                await self.async_set_unique_id(str(serial).strip())
                self._abort_if_unique_id_configured(updates={CONF_HOST: user_input[CONF_HOST]})
                return self.async_create_entry(title=f"HyCube {serial}", data=user_input)
        schema = vol.Schema(
            {
                vol.Required(CONF_HOST): str,
                vol.Required(CONF_USERNAME, default=DEFAULT_USERNAME): str,
                vol.Required(CONF_PASSWORD, default=DEFAULT_PASSWORD): str,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, **user_input}
            info, error = await self._validate(data)
            if error:
                errors["base"] = error
            else:
                # A new address must still point to the same device.
                if serial := _serial(info):
                    await self.async_set_unique_id(serial)
                    self._abort_if_unique_id_mismatch()
                return self.async_update_reload_and_abort(entry, data=data)
        schema = vol.Schema(
            {
                vol.Required(CONF_HOST): str,
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
            }
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(schema, entry.data),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, **user_input}
            _, error = await self._validate(data)
            if error:
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(entry, data=data)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME, default=entry.data[CONF_USERNAME]): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> HyCubeOptionsFlow:
        return HyCubeOptionsFlow()


def _serial(info: Mapping[str, Any]) -> str | None:
    serial = info.get("HYCUBE_SERIAL") or info.get("HYCUBE SERIAL")
    return str(serial).strip() if serial else None


def _seconds(minimum: int, maximum: int) -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=minimum, max=maximum, step=1, unit_of_measurement="s",
            mode=selector.NumberSelectorMode.BOX,
        )
    )


def _number(
    minimum: float, maximum: float, step: float, unit: str | None = None
) -> selector.NumberSelector:
    config = selector.NumberSelectorConfig(
        min=minimum, max=maximum, step=step, mode=selector.NumberSelectorMode.BOX
    )
    if unit:
        config["unit_of_measurement"] = unit
    return selector.NumberSelector(config)


_MULTILINE = selector.TextSelector(selector.TextSelectorConfig(multiline=True))


class HyCubeOptionsFlow(OptionsFlow):
    """Device settings (polling, battery, profile, control) and the schedule."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=["device", "schedule"])

    def _save(self, user_input: dict[str, Any], keys: tuple[str, ...]) -> ConfigFlowResult:
        """Replace the options of one step; a field left empty is removed."""
        options = {k: v for k, v in self.config_entry.options.items() if k not in keys}
        options.update(user_input)
        return self.async_create_entry(data=options)

    async def async_step_schedule(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            for key in (CONF_GRID_CHARGE_POWER, CONF_MAX_CHARGE_SOC, CONF_STANDBY_POWER):
                user_input[key] = int(user_input[key])
            return self._save(user_input, SCHEDULE_OPTIONS)

        opts = self.config_entry.options
        try:
            homes = sorted(await async_tibber_prices(self.hass))
        except Exception:  # noqa: BLE001 - Tibber missing or not reachable
            homes = []
        home: selector.Selector = selector.TextSelector()
        if homes:
            home = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=homes, custom_value=True,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )
        schema = vol.Schema(
            {
                vol.Optional(CONF_TIBBER_HOME): home,
                vol.Required(
                    CONF_PLAN_GRID_CHARGE, default=opts.get(CONF_PLAN_GRID_CHARGE, True)
                ): selector.BooleanSelector(),
                vol.Required(
                    CONF_PLAN_HOLD, default=opts.get(CONF_PLAN_HOLD, True)
                ): selector.BooleanSelector(),
                vol.Required(
                    CONF_GRID_CHARGE_POWER,
                    default=opts.get(CONF_GRID_CHARGE_POWER, DEFAULT_GRID_CHARGE_POWER),
                ): _number(500, 10000, 100, "W"),
                vol.Required(
                    CONF_MAX_CHARGE_SOC,
                    default=opts.get(CONF_MAX_CHARGE_SOC, DEFAULT_MAX_CHARGE_SOC),
                ): _number(20, 100, 5, "%"),
                vol.Optional(CONF_EFFICIENCY): _number(50, 100, 1, "%"),
                vol.Required(
                    CONF_STANDBY_POWER,
                    default=opts.get(CONF_STANDBY_POWER, DEFAULT_STANDBY_POWER),
                ): _number(0, 200, 5, "W"),
                vol.Required(
                    CONF_SHIFT_COST, default=opts.get(CONF_SHIFT_COST, DEFAULT_SHIFT_COST)
                ): _number(0, 20, 0.1, "ct/kWh"),
                vol.Required(
                    CONF_EXPORT_PRICE,
                    default=opts.get(CONF_EXPORT_PRICE, DEFAULT_EXPORT_PRICE),
                ): _number(0, 50, 0.1, "ct/kWh"),
                vol.Required(
                    CONF_PV_AUTO_CORRECT, default=opts.get(CONF_PV_AUTO_CORRECT, True)
                ): selector.BooleanSelector(),
                vol.Required(
                    CONF_PV_FACTOR, default=opts.get(CONF_PV_FACTOR, DEFAULT_PV_FACTOR)
                ): _number(0.3, 1.5, 0.05),
            }
        )
        return self.async_show_form(
            step_id="schedule",
            data_schema=self.add_suggested_values_to_schema(schema, opts),
        )

    async def async_step_device(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            for key in (CONF_SCAN_INTERVAL, CONF_STATUS_INTERVAL):
                user_input[key] = int(user_input[key])
            return self._save(user_input, DEVICE_OPTIONS)

        opts = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL, default=opts.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
                ): _seconds(MIN_SCAN_INTERVAL, 120),
                vol.Required(
                    CONF_STATUS_INTERVAL,
                    default=opts.get(CONF_STATUS_INTERVAL, DEFAULT_STATUS_INTERVAL),
                ): _seconds(15, 3600),
                vol.Required(
                    CONF_BATTERY_CAPACITY,
                    default=opts.get(CONF_BATTERY_CAPACITY, DEFAULT_BATTERY_CAPACITY),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1, max=100, step=0.1, unit_of_measurement="kWh",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(CONF_PROFILE_SOURCE): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor", device_class="energy")
                ),
                vol.Optional(CONF_TEMPERATURE_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor", device_class="temperature")
                ),
                vol.Optional(CONF_WEATHER_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="weather")
                ),
                vol.Required(
                    CONF_CONTROL_ENABLED, default=opts.get(CONF_CONTROL_ENABLED, True)
                ): selector.BooleanSelector(),
                vol.Optional(CONF_CMD_STANDARD): _MULTILINE,
                vol.Optional(CONF_CMD_HOLD): _MULTILINE,
                vol.Optional(CONF_CMD_GRID_CHARGE): _MULTILINE,
            }
        )
        return self.async_show_form(
            step_id="device",
            data_schema=self.add_suggested_values_to_schema(
                schema,
                {
                    CONF_CMD_STANDARD: DEFAULT_CMD_STANDARD,
                    CONF_CMD_HOLD: DEFAULT_CMD_HOLD,
                    CONF_CMD_GRID_CHARGE: DEFAULT_CMD_GRID_CHARGE,
                    **opts,
                },
            ),
            description_placeholders={
                "soc": "{soc}",
                "current_soc": "{current_soc}",
                "hold_active": "{hold_active}",
            },
        )
