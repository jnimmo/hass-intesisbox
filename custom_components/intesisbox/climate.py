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
from homeassistant.components.climate import PLATFORM_SCHEMA, ClimateDevice
from homeassistant.components.climate.const import (ATTR_OPERATION_MODE,
                                                    STATE_AUTO, STATE_COOL,
                                                    STATE_DRY, STATE_FAN_ONLY,
                                                    STATE_HEAT,
                                                    SUPPORT_FAN_MODE,
                                                    SUPPORT_OPERATION_MODE,
                                                    SUPPORT_SWING_MODE,
                                                    SUPPORT_TARGET_TEMPERATURE)
from homeassistant.const import (ATTR_TEMPERATURE, CONF_HOST, STATE_OFF,
                                 STATE_UNKNOWN, TEMP_CELSIUS)
from homeassistant.exceptions import PlatformNotReady

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = 'Intesisbox'

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Required(CONF_HOST): cv.string,
})

# Return cached results if last scan time was less than this value.
# If a persistent connection is established for the controller, changes to
# values are in realtime.
SCAN_INTERVAL = timedelta(seconds=300)

MAP_OPERATION_MODE_TO_HA = {
    'AUTO': STATE_AUTO,
    'FAN': STATE_FAN_ONLY,
    'HEAT': STATE_HEAT,
    'DRY': STATE_DRY,
    'COOL': STATE_COOL,
    'OFF': STATE_OFF
}
MAP_OPERATION_MODE_TO_IB = dict(map(reversed, MAP_OPERATION_MODE_TO_HA.items()))

MAP_STATE_ICONS = {
    STATE_HEAT: 'mdi:white-balance-sunny',
    STATE_AUTO: 'mdi:cached',
    STATE_COOL: 'mdi:snowflake',
    STATE_DRY: 'mdi:water-off',
    STATE_FAN_ONLY: 'mdi:fan',
}

SWING_ON = 'SWING'
SWING_STOP = 'AUTO'
SWING_LIST_HORIZONTAL = 'Horizontal'
SWING_LIST_VERTICAL = 'Vertical'
SWING_LIST_BOTH = 'Both'
SWING_LIST_STOP = 'Auto'

async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Create the IntesisHome climate devices."""
    from . import intesisbox
    controller = intesisbox.IntesisBox(config[CONF_HOST], loop=hass.loop)
    controller.connect()
    while not controller.is_connected:
        await asyncio.sleep(0.1)

    controller.poll_status()
    async_add_entities([IntesisBoxAC(controller)],True)


class IntesisBoxAC(ClimateDevice):
    """Represents an Intesishome air conditioning device."""

    def __init__(self, controller):
        """Initialize the thermostat."""
        _LOGGER.debug('Added climate device with state')
        self._controller = controller

        self._deviceid = controller.device_mac_address
        self._devicename = DEFAULT_NAME
        self._connected = False

        self._max_temp = controller.max_setpoint
        self._min_temp = controller.min_setpoint
        self._target_temp = None
        self._current_temp = None
        self._rssi = None
        self._swing_list = [SWING_LIST_STOP]
        self._vswing = False
        self._hswing = False
        self._power = False
        self._current_operation = STATE_UNKNOWN
        self._connection_retries = 0
        self._has_swing_control = self._controller.has_swing_control

        # Setup fan list
        self._fan_list = [x.title() for x in self._controller.fan_speed_list]
        self._fan_speed = None

        # Setup operation list
        self._operation_list = [STATE_OFF]
        for operation in self._controller.operation_list:
            self._operation_list.append(MAP_OPERATION_MODE_TO_HA[operation])
        
        # Setup feature support
        self._base_features = (SUPPORT_OPERATION_MODE | SUPPORT_TARGET_TEMPERATURE)
        if len(self._fan_list) > 0:
            self._base_features |= SUPPORT_FAN_MODE
        
        # Setup swing control
        if self._has_swing_control:
            self._base_features |= SUPPORT_SWING_MODE
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
    def temperature_unit(self):
        """Intesishome API uses celsius on the backend."""
        return TEMP_CELSIUS

    @property
    def device_state_attributes(self):
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
        _LOGGER.debug("IntesisHome Set Temperature=%s")

        temperature = kwargs.get(ATTR_TEMPERATURE)
        operation_mode = kwargs.get(ATTR_OPERATION_MODE)

        if operation_mode:
            self.set_operation_mode(operation_mode)

        if temperature:
            self._controller.set_temperature(temperature)

    def set_operation_mode(self, operation_mode):
        """Set operation mode."""
        _LOGGER.debug("IntesisHome Set Mode=%s", operation_mode)
        if operation_mode == STATE_OFF:
            self._controller.set_power_off()
            self._power = False
        else:
            self._controller.set_mode(MAP_OPERATION_MODE_TO_IB[operation_mode])

            # Send the temperature again in case changing modes has changed it
            if self._target_temp:
                self._controller.set_temperature(self._target_temp)

        self.hass.async_add_job(self.schedule_update_ha_state, False)

    def turn_on(self):
        """Turn thermostat on."""
        self._controller.set_power_on()
        self.hass.async_add_job(self.schedule_update_ha_state, False)

    def turn_off(self):
        """Turn thermostat off."""
        self.set_operation_mode(STATE_OFF)

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
            await self.hass.async_add_executor_job(self._controller.connect)
            self._connection_retries += 1
        else:
            self._connection_retries = 0

        self._power = self._controller.is_on
        self._current_temp = self._controller.ambient_temperature
        self._min_temp = self._controller.min_setpoint
        self._max_temp = self._controller.max_setpoint
        self._target_temp = self._controller.setpoint
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
                _LOGGER.debug("Lost connection to IntesisHome.")
            else:
                _LOGGER.debug("Connection to IntesisHome was restored.")

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
        _LOGGER.debug("IntesisHome sent a status update.")
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
        """Poll for updates if pyIntesisHome doesn't have a socket open."""
        # This could be switched on controller.is_connected, but HA doesn't
        # seem to handle dynamically changing from push to poll.
        return True

    @property
    def operation_list(self):
        """List of available operation modes."""
        return self._operation_list

    @property
    def current_fan_mode(self):
        """Return whether the fan is on."""
        return self._fan_speed

    @property
    def current_swing_mode(self):
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
    def fan_list(self):
        """List of available fan modes."""
        return self._fan_list

    @property
    def swing_list(self):
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
    def current_operation(self):
        """Return the current mode of operation if unit is on."""
        if self._power:
            return self._current_operation
        return STATE_OFF

    @property
    def target_temperature(self):
        """Return the current setpoint temperature if unit is on."""
        return self._target_temp

    @property
    def supported_features(self):
        """Return the list of supported features."""
        return self._base_features


