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

class IHConnectionError(Exception):
    pass

class IntesisBox(asyncio.Protocol):
    def __init__(self, ip, port=3310, loop=None):
        self._ip = ip
        self._port = port
        self._mac = None
        self._device = {}
        self._connectionStatus = API_DISCONNECTED
        self._connectionRetries = 0
        self._writer = None
        self._reader = None
        self._sendQueueTask = None
        self._updateCallbacks = []
        self._errorCallbacks = []
        self._errorMessage = None
        self._controllerType = None
        self._model: str = None
        self._firmversion: str = None
        self._rssi: int = None

        # Limits
        self._operation_list = []
        self._fan_speed_list = []
        self._vertical_vane_list = []
        self._horizontal_vane_list = []
        self._setpoint_minimum = None
        self._setpoint_maximum = None

        if loop:
            _LOGGER.debug("Using the provided event loop")
            self._eventLoop = loop
        else:
            _LOGGER.debug("Getting the running loop from asyncio")
            self._eventLoop = asyncio.get_running_loop()

    async def _handle_packets(self):
        data = True
        while data:
            try:
                data = await self._reader.readuntil(b"\r\n")
                if not data:
                    break
                message = data.decode("ascii")
                await self.data_received(message)
            except (asyncio.IncompleteReadError, TimeoutError, ConnectionResetError, OSError) as e:
                _LOGGER.error(
                    "Lost connection to Intesisbox %s. Exception: %s", self._ip, e
                )
                break
        self._connectionStatus = API_DISCONNECTED
        self._reader = None
        self._writer = None
        await self._send_update_callback()

    async def data_received(self, decoded_data):
        """asyncio callback when data is received on the socket"""
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
                elif cmd == 'CHN,1':
                    self._parse_change_received(args)
                elif cmd == 'LIMITS':
                    self._parse_limits_received(args)

        await self._send_update_callback()

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

    async def connect(self):
        """Public method for connecting to IntesisHome API"""
        if not (self._ip and self._port):
            _LOGGER.debug("Missing IP address or port.")

        self._connectionRetries = 0
        while self._connectionStatus == API_DISCONNECTED:
            self._connectionStatus = API_CONNECTING
            if self._connectionRetries > 0:
                _LOGGER.debug(
                    "Couldn't connect to IntesisBox, retrying in %i minutes", self._connectionRetries
                )
                await asyncio.sleep(self._connectionRetries * 60)
            try:
                # Create asyncio socket
                self._reader, self._writer = await asyncio.open_connection(
                    self._ip, self._port
                )
                _LOGGER.debug('Opening connection to IntesisBox %s:%s',
                            self._ip, self._port)
                self._eventLoop.create_task(self._handle_packets())

            except Exception as ex:
                _LOGGER.debug('%s Exception. %s / %s', type(ex), repr(ex.args), ex)
                self._connectionRetries += 1
                self._connectionStatus = API_DISCONNECTED
                # raise IHConnectionError from ex

    async def stop(self):
        """Public method for shutting down connectivity with the IntesisBox"""
        self._connectionStatus = API_DISCONNECTED
        if self._writer:
            self._writer._transport.close()

        if self._reader:
            self._reader._transport.close()

    async def poll_status(self, sendcallback=False):
        self._writer.write("GET,1:*\r".encode('ascii'))
        await self._writer.drain()

        if len(self._operation_list) < 1: 
            LIMITS = ["LIMITS:SETPTEMP","LIMITS:FANSP","LIMITS:MODE","LIMITS:VANEUD","LIMITS:VANELR","ID"]
            for limit in LIMITS:
                self._writer.write((limit + '\r').encode('ascii'))
                await self._writer.drain()

    async def set_temperature(self, setpoint):
        """Public method for setting the temperature"""
        set_temp = int(setpoint * 10)
        await self._set_value(FUNCTION_SETPOINT, set_temp)

    async def set_fan_speed(self, fan_speed):
        """Public method to set the fan speed"""
        await self._set_value(FUNCTION_FANSP, fan_speed)

    async def set_vertical_vane(self, vane: str):
        """Public method to set the vertical vane"""
        await self._set_value(FUNCTION_VANEUD, vane)

    async def set_horizontal_vane(self, vane: str):
        """Public method to set the horizontal vane"""
        await self._set_value(FUNCTION_VANELR, vane)

    async def _set_value(self, uid, value):
        """Internal method to send a command to the API"""
        message = "SET,{}:{},{}\r".format(1, uid, value)
        try:
            self._writer.write(message.encode('ascii'))
            await self._writer.drain()
            _LOGGER.debug("Data sent: {!r}".format(message))
        except Exception as e:
            _LOGGER.error('%s Exception. %s / %s', type(e), e.args, e)

    async def set_mode(self, mode):
        if not self.is_on:
            await self.set_power_on()

        if mode in MODES:
            await self._set_value(FUNCTION_MODE, mode)

    async def set_mode_dry(self):
        """Public method to set device to dry asynchronously."""
        await self._set_value(FUNCTION_MODE, MODE_DRY)

    async def set_power_off(self):
        """Public method to turn off the device asynchronously."""
        await self._set_value(FUNCTION_ONOFF, POWER_OFF)

    async def set_power_on(self):
        """Public method to turn on the device asynchronously."""
        await self._set_value(FUNCTION_ONOFF, POWER_ON)

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
            temperature = self.twos_complement_16bit(int(temperature)) / 10
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

    async def _send_update_callback(self):
        """Internal method to notify all update callback subscribers."""
        if self._updateCallbacks == []:
            _LOGGER.debug("Update callback has not been set by client.")

        for callback in self._updateCallbacks:
            await callback()

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

    async def add_update_callback(self, method):
        """Public method to add a callback subscriber."""
        self._updateCallbacks.append(method)

    async def keep_alive(self):
        """Send a keepalive command to reset it's watchdog timer."""
        await asyncio.sleep(10, loop=self._eventLoop)

    @staticmethod 
    def twos_complement_16bit(val):
        """Internal method to compute Two's Complement, to represent negative temperatures"""
        if (val & (1 << 15)) != 0:
            val = val - (1 << 16)
        return val   
