import asyncio
import logging
import requests
import json
import queue
import sys
from optparse import OptionParser
from asyncio import ensure_future

_LOGGER = logging.getLogger('pyintesishome')

API_DISCONNECTED = "Disconnected"
API_CONNECTING = "Connecting"
API_AUTHENTICATED = "Connected"
API_AUTH_FAILED = "Wrong username/password"

MODE_AUTO = 'AUTO'
MODE_DRY = 'DRY'
MODE_FAN = 'FAN'
MODE_COOL = 'COOL'
MODE_HEAT = 'HEAT'

FUNCTION_ONOFF = 'ONOFF'
FUNCTION_MODE = 'MODE'
FUNCTION_SETPOINT = 'SETPTEMP'
FUNCTION_FANSP = 'FANSP'
FUNCTION_VANEUD = 'VANEUD'
FUNCTION_VANELR = 'VANELR'
FUNCTION_AMBTEMP = 'AMBTEMP'
FUNCTION_ERRSTATUS = 'ERRSTATUS'
FUNCTION_ERRCODE = 'ERRCODE'

INTESIS_MAP = {
    'ONOFF': {'name': 'power',
              'values': {'OFF': 'off', 'ON': 'on'}},
    'MODE': {'name': 'mode',
             'values': {'AUTO': 'auto', 'HEAT': 'heat', 'DRY': 'dry', 
                        'FAN': 'fan', 'COOL': 'cool'}},
    'FANSP': {'name': 'fan_speed',
              'values': {'AUTO': 'auto', 1: 'quiet',
                         2: 'low', 3: 'medium',
                         4: 'high'}},
    'VANEUD': {'name': 'vvane',
               'values': {'AUTO': "auto/stop", 'SWING': "swing",
                          1: "manual1", 2: "manual2",
                          3: "manual3", 4: "manual4",
                          5: "manual5"
                          }},
    'VANELR': {'name': 'hvane',
               'values': {'AUTO': "auto/stop", 'SWING': "swing", 1: "manual1",
                          2: "manual2", 3: "manual3", 
                          4: "manual4", 5: "manual5"}},
    'SETPTEMP': {'name': 'setpoint', 'null': 32768},
    'AMBTEMP': {'name': 'temperature'},
}

COMMAND_MAP = {
    'power': {'uid': 1, 
              'values': {'off': 0, 'on': 1}},
    'mode': {'uid': 2,
             'values': {'auto': 0, 'heat': 1, 'dry': 2, 'fan': 3, 'cool': 4}},
    'fan_speed': {'uid': 4,
                  'values': {'auto': 0, 'quiet': 1, 'low': 2, 'medium': 3,
                             'high': 4}},
    'setpoint': {'uid': 'SETPTEMP'}
}


class IntesisBox(asyncio.Protocol):
    def __init__(self, ip, port=3310, password=None, loop=None):
        self._password = password
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

        if loop:
            _LOGGER.debug("Latching onto an existing event loop.")
            self._eventLoop = loop
            self._ownLoop = False
        else:
            _LOGGER.debug("Creating our own event loop.")
            self._eventLoop = asyncio.new_event_loop()
            self._ownLoop = True

    def connection_made(self, transport):
        """asyncio callback for a successful connection."""
        _LOGGER.debug("Connected to IntesisBox")
        self._transport = transport

        # Authenticate
        if self._password:
            authentication = "LOGIN:{}\r".format(self._password)
            self._transport.write(authentication.encode('ascii'))
            _LOGGER.debug("Data sent: {!r}".format(authentication))

        self._transport.write("ID\r".encode('ascii'))
        self._transport.write("LIMITS:SETPTEMP\r".encode('ascii'))

    def data_received(self, data):
        """asyncio callback when data is received on the socket"""
        linesReceived = data.decode('ascii').splitlines()
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

        self._update_device_state(function, value)
        self._send_update_callback()

    def _parse_limits_received(self, args):
        function = args.split(',')[0]
        value = args.split(',',1)[1]

        if function == FUNCTION_SETPOINT:
            min_max = value[1:-1].split(',')
            self._device['setpoint_min'] = min_max[0]
            self._device['setpoint_max'] = min_max[1]

    def connection_lost(self, exc):
        """asyncio callback for a lost TCP connection"""
        self._connectionStatus = API_DISCONNECTED
        _LOGGER.info('The server closed the connection')
        self._send_update_callback()
        if self._ownLoop:
            _LOGGER.info('Stop the event loop')
            self._eventLoop.stop()

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

                    if self._ownLoop:
                        _LOGGER.debug("Starting IntesisHome event loop.")
                        self._eventLoop.run_until_complete(coro)
                        self._eventLoop.run_forever()
                        self._eventLoop.close()
                        _LOGGER.debug("Connection closed.")
                else:
                    _LOGGER.debug("Missing IP address or port.")

            except Exception as e:
                _LOGGER.error('%s Exception. %s / %s', type(e), repr(e.args), e)
                self._connectionStatus = API_DISCONNECTED

    def stop(self):
        """Public method for shutting down connectivity with the envisalink."""
        self._connectionStatus = API_DISCONNECTED
        self._transport.close()

        if self._ownLoop:
            _LOGGER.info("Shutting down IntesisHome client connection...")
            self._eventLoop.call_soon_threadsafe(self._eventLoop.stop)
        else:
            _LOGGER.info("An event loop was given to us- we will shutdown when that event loop shuts down.")

    def poll_status(self, sendcallback=False):
        self._transport.write("GET,1:*\r".encode('ascii'))
        self._transport.write("GET,1:*\r".encode('ascii'))

    def get_run_hours(self) -> str:
        return None

    def set_temperature(self, setpoint):
        """Public method for setting the temperature"""
        set_temp = int(setpoint * 10)
        self._set_value(FUNCTION_SETPOINT, set_temp)

    def set_fan_speed(self, fan: str):
        """Public method to set the fan speed"""
        self._set_value(COMMAND_MAP['fan_speed']['uid'],
                        COMMAND_MAP['fan_speed']['values'][fan])

    def set_vertical_vane(self, vane: str):
        """Public method to set the vertical vane"""
        self._set_value(COMMAND_MAP['vvane']['uid'],
                        COMMAND_MAP['vvane']['values'][vane])

    def set_horizontal_vane(self, vane: str):
        """Public method to set the horizontal vane"""
        self._set_value(COMMAND_MAP['hvane']['uid'],
                        COMMAND_MAP['hvane']['values'][vane])

    def _set_value(self, uid, value):
        """Internal method to send a command to the API"""
        message = "SET,{}:{},{}".format(1, uid, value)
        if self._connectionStatus == API_AUTHENTICATED:
            try:
                self._transport.write(message.encode('ascii'))
                _LOGGER.debug("Data sent: {!r}".format(message))
            except Exception as e:
                _LOGGER.error('%s Exception. %s / %s', type(e), e.args, e)
        else:
            _LOGGER.debug("Added message to queue {!r}".format(message))
            self._commandQueue.put(message)
            if self._connectionStatus == API_DISCONNECTED:
                self.connect()

    def _dequeue(self):
        """Internal method to send the command queue to the API"""
        _LOGGER.debug("Dequeue")

        while not self._commandQueue.empty():
            cmd = self._commandQueue.get_nowait()
            if cmd:
                _LOGGER.debug("Sending from queue: {!r}".format(cmd))
                self._transport.write(cmd.encode('ascii'))

    def _update_device_state(self, uid, value):
        """Internal method to update the state table of IntesisHome devices"""

        if uid in INTESIS_MAP:
            if 'values' in INTESIS_MAP[uid]:
                self._device[INTESIS_MAP[uid]['name']] = INTESIS_MAP[uid]['values'][value]
            elif 'null' in INTESIS_MAP[uid] and value == INTESIS_MAP[uid]['null']:
                self._device[INTESIS_MAP[uid]['name']] = None
            else:
                self._device[INTESIS_MAP[uid]['name']] = value
                _LOGGER.debug(self._device)

    def _update_rssi(self, rssi):
        """Internal method to update the wireless signal strength."""
        if rssi:
            self._device['rssi'] = rssi

    def set_mode_heat(self):
        """Public method to set device to heat asynchronously."""
        if not self.is_on:
            self.set_power_on()
        self._set_value(FUNCTION_MODE, MODE_HEAT)

    def set_mode_cool(self):
        """Public method to set device to cool asynchronously."""
        if not self.is_on:
            self.set_power_on()
        self._set_value(FUNCTION_MODE, MODE_COOL)

    def set_mode_fan(self):
        """Public method to set device to fan asynchronously."""
        if not self.is_on:
            self.set_power_on()
        self._set_value(FUNCTION_MODE, MODE_FAN)

    def set_mode_auto(self):
        """Public method to set device to auto asynchronously."""
        if not self.is_on:
            self.set_power_on()
        self._set_value(FUNCTION_MODE, MODE_AUTO)

    def set_mode_dry(self):
        """Public method to set device to dry asynchronously."""
        if not self.is_on:
            self.set_power_on()
        self._set_value(FUNCTION_MODE, MODE_DRY)

    def set_power_off(self):
        """Public method to turn off the device asynchronously."""
        self._set_value(FUNCTION_ONOFF, 'OFF')

    def set_power_on(self):
        """Public method to turn on the device asynchronously."""
        self._set_value(FUNCTION_ONOFF, 'ON')

    @property
    def mode(self) -> str:
        """Public method returns the current mode of operation."""
        return self._device.get('mode')

    def get_fan_speed(self) -> str:
        """Public method returns the current fan speed."""
        return self._device.get('fan_speed')

    def get_device_name(self) -> str:
        return self._mac

    def get_device_mac(self) -> str:
        return self._mac

    def get_power_state(self) -> str:
        """Public method returns the current power state."""
        return self._device.get('power')

    @property
    def is_on(self) -> bool:
        """Return true if the controlled device is turned on"""
        return self._device.get('power') == 'on'

    def has_swing_control(self) -> bool:
        """Return true if the device supports swing modes."""
        return False

    @property
    def setpoint(self) -> float:
        """Public method returns the target temperature."""
        setpoint = self._device.get('setpoint')
        if setpoint:
            setpoint = int(setpoint) / 10
        return setpoint

    @property
    def ambient_temperature(self) -> float:
        """Public method returns the current temperature."""
        temperature = self._device.get('temperature')
        if temperature:
            temperature = int(temperature) / 10
        return temperature

    @property
    def max_setpoint(self) -> float:
        """Public method returns the current maximum target temperature."""
        temperature = self._device.get('setpoint_max')
        if temperature:
            temperature = int(temperature) / 10
        return temperature

    @property
    def min_setpoint(self) -> float:
        """Public method returns the current minimum target temperature."""
        temperature = self._device.get('setpoint_min')
        if temperature:
            temperature = int(temperature) / 10
        return temperature

    def get_rssi(self) -> str:
        """Public method returns the current wireless signal strength."""
        rssi = self._device.get('rssi')
        return rssi

    def get_vertical_swing(self) -> str:
        """Public method returns the current vertical vane setting."""
        swing = self._device.get('vvane')
        return swing

    def get_horizontal_swing(self) -> str:
        """Public method returns the current horizontal vane setting."""
        swing = self._device.get('hvane')
        return swing

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

    @asyncio.coroutine
    def keep_alive(self):
        """Send a keepalive command to reset it's watchdog timer."""
        yield from asyncio.sleep(10, loop=self._eventLoop)
