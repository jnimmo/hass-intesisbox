"""
Support for IntesisBox Smart AC Controllers.

For more details about this platform, please refer to the documentation at
https://github.com/jnimmo/hass-intesisbox
"""
import asyncio
import logging
from datetime import timedelta
import voluptuous as vol

import homeassistant.helpers.config_validation as cv

from homeassistant.components import persistent_notification
from homeassistant.components.climate import PLATFORM_SCHEMA, ClimateEntity
from homeassistant.components.climate.const import (
    ATTR_HVAC_MODE,
    HVAC_MODE_HEAT_COOL,
    HVAC_MODE_COOL,
    HVAC_MODE_DRY,
    HVAC_MODE_FAN_ONLY,
    HVAC_MODE_HEAT,
    HVAC_MODE_OFF,
    SUPPORT_FAN_MODE,
    SUPPORT_SWING_MODE,
    SUPPORT_TARGET_TEMPERATURE
    )
from homeassistant.const import (
    ATTR_TEMPERATURE,
    CONF_HOST,
    CONF_NAME,
    CONF_UNIQUE_ID,
    STATE_UNKNOWN,
    TEMP_CELSIUS
    )
from homeassistant.exceptions import PlatformNotReady

from . import DOMAIN, PLATFORMS

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = 'Intesisbox'

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Required(CONF_HOST): cv.string,
    vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    vol.Optional(CONF_UNIQUE_ID): cv.string,
})

# Return cached results if last scan time was less than this value.
# If a persistent connection is established for the controller, changes to
# values are in realtime.
SCAN_INTERVAL = timedelta(seconds=300)

MAP_OPERATION_MODE_TO_HA = {
    'AUTO': HVAC_MODE_HEAT_COOL,
    'FAN': HVAC_MODE_FAN_ONLY,
    'HEAT': HVAC_MODE_HEAT,
    'DRY': HVAC_MODE_DRY,
    'COOL': HVAC_MODE_COOL,
    'OFF': HVAC_MODE_OFF
}
MAP_OPERATION_MODE_TO_IB = dict(map(reversed, MAP_OPERATION_MODE_TO_HA.items()))

MAP_STATE_ICONS = {
    HVAC_MODE_HEAT: 'mdi:white-balance-sunny',
    HVAC_MODE_HEAT_COOL: 'mdi:cached',
    HVAC_MODE_COOL: 'mdi:snowflake',
    HVAC_MODE_DRY: 'mdi:water-off',
    HVAC_MODE_FAN_ONLY: 'mdi:fan',
}

SWING_ON = 'SWING'
SWING_STOP = 'AUTO'
SWING_LIST_HORIZONTAL = 'Horizontal'
SWING_LIST_VERTICAL = 'Vertical'
SWING_LIST_BOTH = 'Both'
SWING_LIST_STOP = 'Auto'

async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Create the Intesisbox climate devices."""
    from . import intesisbox
    controller = intesisbox.IntesisBox(config[CONF_HOST], loop=hass.loop)
    controller.connect()
    while not controller.is_connected:
        await asyncio.sleep(0.1)

    name = config.get(CONF_NAME)
    unique_id = config.get(CONF_UNIQUE_ID)
    async_add_entities([IntesisBoxAC(controller, name, unique_id)],True)

async def async_setup_entry(hass, entry, async_add_entities):
    controller = hass.data[DOMAIN][entry.entry_id]
    controller.poll_status()
    async_add_entities([IntesisBoxAC(controller)], True)

class IntesisBoxAC(ClimateEntity):
    """Represents an Intesisbox air conditioning device."""

    def __init__(self, controller, name = None, unique_id = None):
        """Initialize the thermostat."""
        _LOGGER.debug('Added climate device with state')
        self._controller = controller

        self._deviceid = controller.device_mac_address
        self._devicename = name or controller.device_mac_address
        self._unique_id = unique_id or controller.device_mac_address
        self._connected = controller.is_connected

        self._max_temp = controller.max_setpoint
        self._min_temp = controller.min_setpoint
        self._target_temperature = None
        self._current_temp = None
        self._rssi = None
        self._swing_list = []
        self._vswing = False
        self._hswing = False
        self._power = False
        self._current_operation = STATE_UNKNOWN
        self._connection_retries = 0
        self._has_swing_control = self._controller.has_swing_control

        # Setup fan list
        self._fan_list = [x.title() for x in self._controller.fan_speed_list]
        if len(self._fan_list) < 1:
          raise PlatformNotReady
        self._fan_speed = None

        # Setup operation list
        self._operation_list = [HVAC_MODE_OFF]
        for operation in self._controller.operation_list:
            self._operation_list.append(MAP_OPERATION_MODE_TO_HA[operation])
        if len(self._operation_list) == 1:
           raise PlatformNotReady

        # Setup feature support
        self._base_features = (SUPPORT_TARGET_TEMPERATURE)
        if len(self._fan_list) > 0:
            self._base_features |= SUPPORT_FAN_MODE

        # Setup swing control
        if self._has_swing_control:
            self._base_features |= SUPPORT_SWING_MODE
            self._swing_list = [SWING_LIST_STOP]
            if SWING_ON in self._controller.vane_horizontal_list:
                self._swing_list.append(SWING_LIST_HORIZONTAL)
            if SWING_ON in self._controller.vane_vertical_list:
                self._swing_list.append(SWING_LIST_VERTICAL)
            if len(self._swing_list) > 2:
                self._swing_list.append(SWING_LIST_BOTH)

        self._controller.add_update_callback(self.update_callback)

    @property
    def name(self):
        """Return the name of the AC device."""
        return self._devicename

    @property
    def unique_id(self):
        """Return the unique id of the AC device."""
        return self._unique_id

    @property
    def temperature_unit(self):
        """Intesisbox API uses celsius on the backend."""
        return TEMP_CELSIUS

    @property
    def device_info(self):
        return {
            "identifiers": { (DOMAIN, self.unique_id) },
            "name": self.name,
            "manufacturer": "Intesis",
            "model": self._controller.device_model,
            "sw_version": self._controller.firmware_version,
        }

    @property
    def extra_state_attributes(self):
        """Return the device specific state attributes."""
        attrs = {}
        if self._has_swing_control:
            attrs['vertical_swing'] = self._vswing
            attrs['horizontal_swing'] = self._hswing

        if self._controller.is_connected:
            attrs['ha_update_type'] = 'push'
        else:
            attrs['ha_update_type'] = 'poll'

        return attrs

    def set_temperature(self, **kwargs):
        """Set new target temperature."""
        _LOGGER.debug("Intesisbox Set Temperature=%s")

        temperature = kwargs.get(ATTR_TEMPERATURE)
        operation_mode = kwargs.get(ATTR_HVAC_MODE)

        if operation_mode:
            self.set_hvac_mode(operation_mode)

        if temperature:
            self._controller.set_temperature(temperature)

    def set_hvac_mode(self, operation_mode):
        """Set operation mode."""
        _LOGGER.debug("Intesisbox Set Mode=%s", operation_mode)
        if operation_mode == HVAC_MODE_OFF:
            self._controller.set_power_off()
            self._power = False
        else:
            self._controller.set_mode(MAP_OPERATION_MODE_TO_IB[operation_mode])

            # Send the temperature again in case changing modes has changed it
            if self._target_temperature:
                self._controller.set_temperature(self._target_temperature)

        self.hass.async_add_job(self.schedule_update_ha_state, False)

    def turn_on(self):
        """Turn thermostat on."""
        self._controller.set_power_on()
        self.hass.async_add_job(self.schedule_update_ha_state, False)

    def turn_off(self):
        """Turn thermostat off."""
        self.set_hvac_mode(HVAC_MODE_OFF)

    def set_fan_mode(self, fan_mode):
        """Set fan mode (from quiet, low, medium, high, auto)."""
        self._controller.set_fan_speed(fan_mode.upper())

    def set_swing_mode(self, swing_mode):
        """Set the vertical vane."""
        if swing_mode == SWING_LIST_BOTH:
            self._controller.set_vertical_vane(SWING_ON)
            self._controller.set_horizontal_vane(SWING_ON)
        elif swing_mode == SWING_LIST_STOP:
            self._controller.set_vertical_vane(SWING_STOP)
            self._controller.set_horizontal_vane(SWING_STOP)
        elif swing_mode == SWING_LIST_HORIZONTAL:
            self._controller.set_vertical_vane(SWING_STOP)
            self._controller.set_horizontal_vane(SWING_ON)
        elif swing_mode == SWING_LIST_VERTICAL:
            self._controller.set_vertical_vane(SWING_ON)
            self._controller.set_horizontal_vane(SWING_STOP)

    async def async_update(self):
        """Copy values from controller dictionary to climate device."""
        if not self._controller.is_connected:
            await asyncio.sleep(1) # per device specs, wait min 1 sec before re-connecting
            await self.hass.async_add_executor_job(self._controller.connect)
            self._connection_retries += 1
        else:
            self._connection_retries = 0

        self._power = self._controller.is_on
        self._current_temp = self._controller.ambient_temperature
        self._min_temp = self._controller.min_setpoint
        self._max_temp = self._controller.max_setpoint
        self._target_temperature = self._controller.setpoint

        if self._controller.fan_speed:
            self._fan_speed = self._controller.fan_speed.title()

        # Operation mode
        ib_mode = self._controller.mode
        self._current_operation = MAP_OPERATION_MODE_TO_HA.get(ib_mode, STATE_UNKNOWN)

        # Swing mode
        # Climate module only supports one swing setting.
        if self._has_swing_control:
            self._vswing = self._controller.vertical_swing() == SWING_ON
            self._hswing = self._controller.horizontal_swing() == SWING_ON

        # Track connection lost/restored.
        if self._connected != self._controller.is_connected:
            self._connected = self._controller.is_connected
            if self._connected:
                _LOGGER.debug("Connection to Intesisbox was restored.")
            else:
                _LOGGER.debug("Lost connection to Intesisbox.")

    async def async_will_remove_from_hass(self):
        """Shutdown the controller when the device is being removed."""
        self._controller.stop()

    @property
    def icon(self):
        """Return the icon for the current state."""
        icon = None
        if self._power:
            icon = MAP_STATE_ICONS.get(self._current_operation)
        return icon

    def update_callback(self):
        """Let HA know there has been an update from the controller."""
        _LOGGER.debug("Intesisbox sent a status update.")
        if self.hass:
            self.hass.async_add_job(self.schedule_update_ha_state, True)

    @property
    def min_temp(self):
        """Return the minimum temperature for the current mode of operation."""
        return self._min_temp

    @property
    def max_temp(self):
        """Return the maximum temperature for the current mode of operation."""
        return self._max_temp

    @property
    def is_on(self):
        """Return true if on."""
        return self._power

    @property
    def should_poll(self):
        """Poll for updates if pyIntesisbox doesn't have a socket open."""
        # This could be switched on controller.is_connected, but HA doesn't
        # seem to handle dynamically changing from push to poll.
        return True

    @property
    def hvac_modes(self):
        """List of available operation modes."""
        return self._operation_list

    @property
    def fan_mode(self):
        """Return whether the fan is on."""
        return self._fan_speed

    @property
    def swing_mode(self):
        """Return current swing mode."""
        if self._vswing and self._hswing:
            return SWING_LIST_BOTH
        elif self._vswing:
            return SWING_LIST_VERTICAL
        elif self._hswing:
            return SWING_LIST_HORIZONTAL
        else:
            return SWING_LIST_STOP

    @property
    def fan_modes(self):
        """List of available fan modes."""
        return self._fan_list

    @property
    def swing_modes(self):
        """List of available swing positions."""
        return self._swing_list

    @property
    def assumed_state(self) -> bool:
        """If the device is not connected we have to assume state."""
        return not self._connected

    @property
    def available(self) -> bool:
        """If the device hasn't been able to connect, mark as unavailable."""
        return self._connected or self._connection_retries < 2

    @property
    def current_temperature(self):
        """Return the current temperature."""
        return self._current_temp

    @property
    def hvac_mode(self):
        """Return the current mode of operation if unit is on."""
        if self._power:
            return self._current_operation
        return HVAC_MODE_OFF

    @property
    def target_temperature(self):
        """Return the current setpoint temperature if unit is on."""
        return self._target_temperature

    @property
    def supported_features(self):
        """Return the list of supported features."""
        return self._base_features
