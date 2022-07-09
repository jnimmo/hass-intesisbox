import asyncio
import logging
import requests
import json
import queue
import sys
from optparse import OptionParser
from asyncio import ensure_future
from time import sleep

_LOGGER = logging.getLogger('pyintesisbox')

API_DISCONNECTED = "Disconnected"
API_CONNECTING = "Connecting"
API_AUTHENTICATED = "Connected"

POWER_ON = 'ON'
POWER_OFF = 'OFF'
POWER_STATES = [POWER_ON, POWER_OFF]

MODE_AUTO = 'AUTO'
MODE_DRY = 'DRY'
MODE_FAN = 'FAN'
MODE_COOL = 'COOL'
MODE_HEAT = 'HEAT'
MODES = [MODE_AUTO, MODE_DRY, MODE_FAN, MODE_COOL, MODE_HEAT]

FUNCTION_ONOFF = 'ONOFF'
FUNCTION_MODE = 'MODE'
FUNCTION_SETPOINT = 'SETPTEMP'
FUNCTION_FANSP = 'FANSP'
FUNCTION_VANEUD = 'VANEUD'
FUNCTION_VANELR = 'VANELR'
FUNCTION_AMBTEMP = 'AMBTEMP'
FUNCTION_ERRSTATUS = 'ERRSTATUS'
FUNCTION_ERRCODE = 'ERRCODE'

NULL_VALUE = '32768'


class IntesisBox(asyncio.Protocol):
    def __init__(self, ip, port=3310, loop=None):
        self._ip = ip
        self._port = port
        self._mac = None
        self._device = {}
        self._connectionStatus = API_DISCONNECTED
        self._commandQueue = queue.Queue()
        self._transport = None
        self._updateCallbacks = []
        self._errorCallbacks = []
        self._errorMessage = None
        self._controllerType = None
        self._model: str = None
        self._firmversion: str = None
        self._rssi: int = None
        self._eventLoop = loop

        # Limits
        self._operation_list = []
        self._fan_speed_list = []
        self._vertical_vane_list = []
        self._horizontal_vane_list = []
        self._setpoint_minimum = None
        self._setpoint_maximum = None

    def connection_made(self, transport):
        """asyncio callback for a successful connection."""
        _LOGGER.debug("Connected to IntesisBox")
        self._transport = transport
        asyncio.ensure_future(self.query_initial_state())

    async def keep_alive(self):
        """Send a keepalive command to reset it's watchdog timer."""
        while self.is_connected:
            _LOGGER.debug("Sending keepalive")
            self._transport.write("PING\r".encode('ascii'))
            await asyncio.sleep(45)
        else:
            _LOGGER.debug("Not connected, skipping keepalive")

    async def query_initial_state(self):
        self._transport.write("ID\r".encode('ascii'))
        await asyncio.sleep(1)
        self._transport.write("LIMITS:SETPTEMP\r".encode('ascii'))
        await asyncio.sleep(1)
        self._transport.write("LIMITS:FANSP\r".encode('ascii'))
        await asyncio.sleep(1)
        self._transport.write("LIMITS:MODE\r".encode('ascii'))
        await asyncio.sleep(1)
        self._transport.write("LIMITS:VANEUD\r".encode('ascii'))
        await asyncio.sleep(1)
        self._transport.write("LIMITS:VANELR\r".encode('ascii'))

    def data_received(self, data):
        """asyncio callback when data is received on the socket"""
        decoded_data = data.decode('ascii')
        _LOGGER.debug("Data received: {}".format(decoded_data))
        linesReceived = decoded_data.splitlines()
        for line in linesReceived:
            cmdList = line.split(':', 1)
            cmd = cmdList[0]
            args = None
            if len(cmdList) > 1:
                args = cmdList[1]
                if cmd == 'ID':
                    self._parse_id_received(args)
                    self._connectionStatus = API_AUTHENTICATED
                    asyncio.ensure_future(self.keep_alive())
                elif cmd == 'CHN,1':
                    self._parse_change_received(args)
                elif cmd == 'LIMITS':
                    self._parse_limits_received(args)

        self._send_update_callback()

    def _parse_id_received(self, args):
        # ID:Model,MAC,IP,Protocol,Version,RSSI
        info = args.split(',')
        if len(info) >= 6:
            self._model = info[0]
            self._mac = info[1]
            self._firmversion = info[4]
            self._rssi = info[5]

    def _parse_change_received(self, args):
        function = args.split(',')[0]
        value = args.split(',')[1]
        if value == NULL_VALUE:
            value = None
        self._device[function] = value

    def _parse_limits_received(self, args):
        split_args = args.split(',', 1)

        if len(split_args) == 2:
            function = split_args[0]
            values = split_args[1][1:-1].split(',')

            if function == FUNCTION_SETPOINT and len(values) == 2:
                self._setpoint_minimum = int(values[0])/10
                self._setpoint_maximum = int(values[1])/10
            elif function == FUNCTION_FANSP:
                self._fan_speed_list = values
            elif function == FUNCTION_MODE:
                self._operation_list = values
            elif function == FUNCTION_VANEUD:
                self._vertical_vane_list = values
            elif function == FUNCTION_VANELR:
                self._horizontal_vane_list = values
        return

    def connection_lost(self, exc):
        """asyncio callback for a lost TCP connection"""
        self._connectionStatus = API_DISCONNECTED
        _LOGGER.info('The server closed the connection')
        self._send_update_callback()

    def connect(self):
        """Public method for connecting to IntesisHome API"""
        if self._connectionStatus == API_DISCONNECTED:
            self._connectionStatus = API_CONNECTING
            try:
                # Must poll to get the authentication token
                if self._ip and self._port:
                    # Create asyncio socket
                    coro = self._eventLoop.create_connection(lambda: self,
                                                             self._ip,
                                                             self._port)
                    _LOGGER.debug('Opening connection to IntesisBox %s:%s',
                                  self._ip, self._port)
                    ensure_future(coro, loop=self._eventLoop)
                else:
                    _LOGGER.debug("Missing IP address or port.")

            except Exception as e:
                _LOGGER.error('%s Exception. %s / %s', type(e), repr(e.args), e)
                self._connectionStatus = API_DISCONNECTED

    def stop(self):
        """Public method for shutting down connectivity with the envisalink."""
        self._connectionStatus = API_DISCONNECTED
        self._transport.close()

    def poll_status(self, sendcallback=False):
        self._transport.write("GET,1:*\r".encode('ascii'))

    def set_temperature(self, setpoint):
        """Public method for setting the temperature"""
        set_temp = int(setpoint * 10)
        self._set_value(FUNCTION_SETPOINT, set_temp)

    def set_fan_speed(self, fan_speed):
        """Public method to set the fan speed"""
        self._set_value(FUNCTION_FANSP, fan_speed)

    def set_vertical_vane(self, vane: str):
        """Public method to set the vertical vane"""
        self._set_value(FUNCTION_VANEUD, vane)

    def set_horizontal_vane(self, vane: str):
        """Public method to set the horizontal vane"""
        self._set_value(FUNCTION_VANELR, vane)

    def _set_value(self, uid, value):
        """Internal method to send a command to the API"""
        message = "SET,{}:{},{}\r".format(1, uid, value)
        try:
            self._transport.write(message.encode('ascii'))
            _LOGGER.debug("Data sent: {!r}".format(message))
        except Exception as e:
            _LOGGER.error('%s Exception. %s / %s', type(e), e.args, e)

    def set_mode(self, mode):
        if not self.is_on:
            self.set_power_on()

        if mode in MODES:
            self._set_value(FUNCTION_MODE, mode)

    def set_mode_dry(self):
        """Public method to set device to dry asynchronously."""
        self._set_value(FUNCTION_MODE, MODE_DRY)

    def set_power_off(self):
        """Public method to turn off the device asynchronously."""
        self._set_value(FUNCTION_ONOFF, POWER_OFF)

    def set_power_on(self):
        """Public method to turn on the device asynchronously."""
        self._set_value(FUNCTION_ONOFF, POWER_ON)

    @property
    def operation_list(self):
        return self._operation_list

    @property
    def vane_horizontal_list(self):
        return self._horizontal_vane_list

    @property
    def vane_vertical_list(self):
        return self._vertical_vane_list

    @property
    def mode(self) -> str:
        """Public method returns the current mode of operation."""
        return self._device.get(FUNCTION_MODE)

    @property
    def fan_speed(self) -> str:
        """Public method returns the current fan speed."""
        return self._device.get(FUNCTION_FANSP)

    @property
    def fan_speed_list(self):
        return self._fan_speed_list

    @property
    def device_mac_address(self) -> str:
        return self._mac

    @property
    def device_model(self) -> str:
        return self._model

    @property
    def firmware_version(self) -> str:
        return self._firmversion

    @property
    def is_on(self) -> bool:
        """Return true if the controlled device is turned on"""
        return self._device.get(FUNCTION_ONOFF) == POWER_ON

    @property
    def has_swing_control(self) -> bool:
        """Return true if the device supports swing modes."""
        return len(self._horizontal_vane_list) > 1 or len(self._vertical_vane_list) > 1

    @property
    def setpoint(self) -> float:
        """Public method returns the target temperature."""
        setpoint = self._device.get(FUNCTION_SETPOINT)
        if setpoint:
            setpoint = int(setpoint) / 10
        return setpoint

    @property
    def ambient_temperature(self) -> float:
        """Public method returns the current temperature."""
        temperature = self._device.get(FUNCTION_AMBTEMP)
        if temperature:
            temperature = int(temperature) / 10
        # When unsupported, -32768 is reported
        if temperature == -3276.8:
            temperature = None
        return temperature

    @property
    def max_setpoint(self) -> float:
        """Public method returns the current maximum target temperature."""
        return self._setpoint_maximum

    @property
    def min_setpoint(self) -> float:
        """Public method returns the current minimum target temperature."""
        return self._setpoint_minimum

    @property
    def rssi(self) -> str:
        """Public method returns the current wireless signal strength."""
        return self._rssi

    def vertical_swing(self) -> str:
        """Public method returns the current vertical vane setting."""
        return self._device.get(FUNCTION_VANEUD)

    def horizontal_swing(self) -> str:
        """Public method returns the current horizontal vane setting."""
        return self._device.get(FUNCTION_VANELR)

    def _send_update_callback(self):
        """Internal method to notify all update callback subscribers."""
        if self._updateCallbacks == []:
            _LOGGER.debug("Update callback has not been set by client.")

        for callback in self._updateCallbacks:
            callback()

    def _send_error_callback(self, message):
        """Internal method to notify all update callback subscribers."""
        self._errorMessage = message

        if self._errorCallbacks == []:
            _LOGGER.debug("Error callback has not been set by client.")

        for callback in self._errorCallbacks:
            callback(message)

    @property
    def is_connected(self) -> bool:
        """Returns true if the TCP connection is established."""
        return self._connectionStatus == API_AUTHENTICATED

    @property
    def error_message(self) -> str:
        """Returns the last error message, or None if there were no errors."""
        return self._errorMessage

    @property
    def is_disconnected(self) -> bool:
        """Returns true when the TCP connection is disconnected and idle."""
        return self._connectionStatus == API_DISCONNECTED

    def add_update_callback(self, method):
        """Public method to add a callback subscriber."""
        self._updateCallbacks.append(method)

    def add_error_callback(self, method):
        """Public method to add a callback subscriber."""
        self._errorCallbacks.append(method)
