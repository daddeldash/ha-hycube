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
    CONF_PROFILE_SOURCE,
    CONF_PROFILE_WEEKS,
    CONF_SCAN_INTERVAL,
    CONF_STATUS_INTERVAL,
    DEFAULT_BATTERY_CAPACITY,
    DEFAULT_CMD_GRID_CHARGE,
    DEFAULT_CMD_HOLD,
    DEFAULT_CMD_STANDARD,
    DEFAULT_PASSWORD,
    DEFAULT_PROFILE_WEEKS,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_STATUS_INTERVAL,
    DEFAULT_USERNAME,
    DOMAIN,
    MIN_SCAN_INTERVAL,
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


_MULTILINE = selector.TextSelector(selector.TextSelectorConfig(multiline=True))


class HyCubeOptionsFlow(OptionsFlow):
    """Polling, battery, profile and control settings."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            for key in (CONF_SCAN_INTERVAL, CONF_STATUS_INTERVAL, CONF_PROFILE_WEEKS):
                user_input[key] = int(user_input[key])
            return self.async_create_entry(data=user_input)

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
                vol.Required(
                    CONF_PROFILE_WEEKS, default=opts.get(CONF_PROFILE_WEEKS, DEFAULT_PROFILE_WEEKS)
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, max=52, step=1, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Optional(CONF_PROFILE_SOURCE): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor", device_class="energy")
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
            step_id="init",
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
