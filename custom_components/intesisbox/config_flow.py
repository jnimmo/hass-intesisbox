"""Config flow to configure the Intesisbox integration."""

import logging

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from . import DOMAIN
from .const import CONF_POWER_SENSOR, CONF_POWER_THRESHOLD, DEFAULT_POWER_THRESHOLD

_LOGGER = logging.getLogger(__name__)


class IntesisboxFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):  # type:ignore
    """Handle a config flow."""

    VERSION = 1

    def __init__(self):
        """Initialize Intesisbox config flow."""
        self._host = None

    @staticmethod
    def async_get_options_flow(config_entry):
        """Return the options flow handler."""
        return IntesisboxOptionsFlowHandler(config_entry)

    def _show_setup_form(self, user_input=None, errors=None):
        """Show the setup form to the user."""

        if user_input is None:
            user_input = {}

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST, default=user_input.get(CONF_HOST, "")): str,
                }
            ),
            errors=errors or {},
        )

    async def async_step_user(self, user_input=None):
        """Handle a flow initiated by the user."""
        errors = {}

        if user_input is None:
            return self._show_setup_form(user_input, errors)

        self._host = user_input[CONF_HOST]

        # Check if already configured
        # await self.async_set_unique_id(self._host)
        # self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=self._host,
            data={CONF_HOST: self._host},
        )

    async def async_step_import(self, user_input=None):
        """Import a config entry."""
        return await self.async_step_user(user_input)


class IntesisboxOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle Intesisbox options."""

    def __init__(self, config_entry):
        """Initialize options flow."""
        self._config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self._config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_POWER_SENSOR,
                        description={
                            "suggested_value": current.get(CONF_POWER_SENSOR)
                        },
                    ): EntitySelector(
                        EntitySelectorConfig(domain="sensor", device_class="power")
                    ),
                    vol.Optional(
                        CONF_POWER_THRESHOLD,
                        default=current.get(
                            CONF_POWER_THRESHOLD, DEFAULT_POWER_THRESHOLD
                        ),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=0,
                            max=10000,
                            step=0.1,
                            unit_of_measurement="W",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                }
            ),
        )
