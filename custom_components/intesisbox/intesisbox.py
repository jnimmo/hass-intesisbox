"""Communication with an Intesisbox device."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging

_LOGGER = logging.getLogger(__name__)

API_DISCONNECTED = "Disconnected"
API_CONNECTING = "Connecting"
API_AUTHENTICATED = "Connected"

POWER_ON = "ON"
POWER_OFF = "OFF"
POWER_STATES = [POWER_ON, POWER_OFF]

MODE_AUTO = "AUTO"
MODE_DRY = "DRY"
MODE_FAN = "FAN"
MODE_COOL = "COOL"
MODE_HEAT = "HEAT"
MODES = [MODE_AUTO, MODE_DRY, MODE_FAN, MODE_COOL, MODE_HEAT]

FUNCTION_ONOFF = "ONOFF"
FUNCTION_MODE = "MODE"
FUNCTION_SETPOINT = "SETPTEMP"
FUNCTION_FANSP = "FANSP"
FUNCTION_VANEUD = "VANEUD"
FUNCTION_VANELR = "VANELR"
FUNCTION_AMBTEMP = "AMBTEMP"
FUNCTION_ERRSTATUS = "ERRSTATUS"
FUNCTION_ERRCODE = "ERRCODE"

NULL_VALUES = ["-32768", "32768"]

background_tasks = set()


def clean_background_task(task):
    """Handle background task completion."""
    background_tasks.discard(task)
    _ = task.result()  # to propagate exceptions


def ensure_background_task(coro, loop):
    """Ensure background task is running."""
    task = asyncio.ensure_future(coro, loop=loop)
    background_tasks.add(task)
    task.add_done_callback(clean_background_task)
    return task


class IntesisBox(asyncio.Protocol):
    """Handles communication with an intesisbox device via WMP."""

    def __init__(self, ip: str, port: int = 3310, loop=None):
        """Set up base state."""
        self._ip = ip
        self._port = port
        self._mac = None
        self._device: dict[str, str] = {}
        self._connectionStatus = API_DISCONNECTED
        self._transport: asyncio.BaseTransport | None = None
        self._updateCallbacks: list[Callable[[], None]] = []
        self._errorCallbacks: list[Callable[[str], None]] = []
        self._errorMessage: str | None = None
        self._controllerType = None
        self._model: str | None = None
        self._firmversion: str | None = None
        self._rssi: int | None = None
        self._eventLoop = loop

        # Limits
        self._operation_list: list[str] = []
        self._fan_speed_list: list[str] = []
        self._vertical_vane_list: list[str] = []
        self._horizontal_vane_list: list[str] = []
        self._setpoint_minimum: int | None = None
        self._setpoint_maximum: int | None = None

    def connection_made(self, transport: asyncio.BaseTransport):
        """Asyncio callback for a successful connection."""
        _LOGGER.debug("Connected to IntesisBox")
        self._transport = transport
        ensure_background_task(self.query_initial_state(), self._eventLoop)

    async def keep_alive(self):
        """Send a keepalive command to reset it's watchdog timer."""
        while self.is_connected:
            _LOGGER.debug("Sending keepalive")
            self._write("PING")
            await asyncio.sleep(45)
        else:
            _LOGGER.debug("Not connected, skipping keepalive")

    async def poll_ambtemp(self):
        """Retrieve Ambient Temperature to prevent integration timeouts."""
        while self.is_connected:
            _LOGGER.debug("Sending AMBTEMP")
            self._write("GET,1:AMBTEMP")
            await asyncio.sleep(10)
        else:
            _LOGGER.debug("Not connected, skipping Ambient Temp Request")

    async def query_initial_state(self):
        """Fetch configuration from the device upon connection."""
        cmds = [
            "ID",
            "LIMITS:SETPTEMP",
            "LIMITS:FANSP",
            "LIMITS:MODE",
            "LIMITS:VANEUD",
            "LIMITS:VANELR",
        ]
        for cmd in cmds:
            self._write(cmd)
            await asyncio.sleep(1)

    def _write(self, cmd):
        self._transport.write(f"{cmd}\r".encode("ascii"))
        _LOGGER.debug(f"Data sent: {cmd!r}")

    async def _writeasync(self, cmd):
        """Async write to slow down commands and await response from units."""
        self._transport.write(f"{cmd}\r".encode("ascii"))
        _LOGGER.debug(f"Data sent: {cmd!r}")
        await asyncio.sleep(1)

    def data_received(self, data):
        """Asyncio callback when data is received on the socket."""
        linesReceived = data.decode("ascii").splitlines()
        statusChanged = False

        for line in linesReceived:
            _LOGGER.debug(f"Data received: {line!r}")
            cmdList = line.split(":", 1)
            cmd = cmdList[0]
            args = None
            if len(cmdList) > 1:
                args = cmdList[1]
                if cmd == "ID":
                    self._parse_id_received(args)
                    self._connectionStatus = API_AUTHENTICATED
                    ensure_background_task(self.poll_status(), self._eventLoop)
                    ensure_background_task(self.poll_ambtemp(), self._eventLoop)
                elif cmd == "CHN,1":
                    self._parse_change_received(args)
                    statusChanged = True
                elif cmd == "LIMITS":
                    self._parse_limits_received(args)
                    statusChanged = True

        if statusChanged:
            self._send_update_callback()

    def _parse_id_received(self, args):
        # ID:Model,MAC,IP,Protocol,Version,RSSI
        info = args.split(",")
        if len(info) >= 6:
            self._model = info[0]
            self._mac = info[1]
            self._firmversion = info[4]
            self._rssi = info[5]

            _LOGGER.debug(
                "Updated info:",
                f"model:{self._model}",
                f"mac:{self._mac}",
                f"version:{self._firmversion}",
                f"rssi:{self._rssi}",
            )

    def _parse_change_received(self, args):
        function = args.split(",")[0]
        value = args.split(",")[1]
        if value in NULL_VALUES:
            value = None
        self._device[function] = value

        _LOGGER.debug(f"Updated state: {self._device!r}")

    def _parse_limits_received(self, args):
        split_args = args.split(",", 1)

        if len(split_args) == 2:
            function = split_args[0]
            values = split_args[1][1:-1].split(",")

            if function == FUNCTION_SETPOINT and len(values) == 2:
                self._setpoint_minimum = int(values[0]) / 10
                self._setpoint_maximum = int(values[1]) / 10
            elif function == FUNCTION_FANSP:
                self._fan_speed_list = values
            elif function == FUNCTION_MODE:
                self._operation_list = values
            elif function == FUNCTION_VANEUD:
                self._vertical_vane_list = values
            elif function == FUNCTION_VANELR:
                self._horizontal_vane_list = values

            _LOGGER.debug(
                "Updated limits: ",
                f"{self._setpoint_minimum=}",
                f"{self._setpoint_maximum=}",
                f"{self._fan_speed_list=}",
                f"{self._operation_list=}",
                f"{self._vertical_vane_list=}",
                f"{self._horizontal_vane_list=}",
            )
        return

    def connection_lost(self, exc):
        """Asyncio callback for a lost TCP connection."""
        self._connectionStatus = API_DISCONNECTED
        _LOGGER.info("The server closed the connection")
        self._send_update_callback()

    def connect(self):
        """Public method for connecting to IntesisHome API."""
        if self._connectionStatus == API_DISCONNECTED:
            self._connectionStatus = API_CONNECTING
            try:
                # Must poll to get the authentication token
                if self._ip and self._port:
                    # Create asyncio socket
                    coro = self._eventLoop.create_connection(
                        lambda: self, self._ip, self._port
                    )
                    _LOGGER.debug(
                        "Opening connection to IntesisBox %s:%s", self._ip, self._port
                    )
                    ensure_background_task(coro, self._eventLoop)
                else:
                    _LOGGER.debug("Missing IP address or port.")
                    self._connectionStatus = API_DISCONNECTED

            except Exception as e:
                _LOGGER.error("%s Exception. %s / %s", type(e), repr(e.args), e)
                self._connectionStatus = API_DISCONNECTED
        elif self._connectionStatus == API_CONNECTING:
            _LOGGER.debug("connect() called but already connecting")
            if self._transport.is_closing():
                _LOGGER.debug(
                    "Socket is closing while trying to connect. Force reconnection"
                )
                self._connectionStatus = API_DISCONNECTED
                self._transport.close()
                self._send_update_callback()

    def stop(self):
        """Public method for shutting down connectivity with the envisalink."""
        self._connectionStatus = API_DISCONNECTED
        self._transport.close()

    async def poll_status(self, sendcallback=False):
        """Periodically poll for updates since the controllers don't always update reliably."""
        while self.is_connected:
            _LOGGER.debug("Polling for update")
            self._write("GET,1:*")
            await asyncio.sleep(60 * 5)  # 5 minutes
        else:
            _LOGGER.debug("Not connected, skipping poll_status()")

    def set_temperature(self, setpoint):
        """Public method for setting the temperature."""
        set_temp = int(setpoint * 10)
        self._set_value(FUNCTION_SETPOINT, set_temp)

    def set_fan_speed(self, fan_speed):
        """Public method to set the fan speed."""
        self._set_value(FUNCTION_FANSP, fan_speed)

    def set_vertical_vane(self, vane: str):
        """Public method to set the vertical vane."""
        self._set_value(FUNCTION_VANEUD, vane)

    def set_horizontal_vane(self, vane: str):
        """Public method to set the horizontal vane."""
        self._set_value(FUNCTION_VANELR, vane)

    def _set_value(self, uid: str, value: str | int) -> None:
        """Change a setting on the thermostat."""
        try:
            asyncio.run(self._writeasync(f"SET,1:{uid},{value}"))
        except Exception as e:
            _LOGGER.error("%s Exception. %s / %s", type(e), e.args, e)

    def set_mode(self, mode):
        """Send mode and confirm change before turning on."""
        """Some units return responses out of order"""
        _LOGGER.debug(f"Setting MODE to {mode}.")
        if mode in MODES:
            self._set_value(FUNCTION_MODE, mode)
        if not self.is_on:
            """Check to ensure in correct mode before turning on"""
            retry = 30
            while self.mode != mode and retry > 0:
                _LOGGER.debug(
                    f"Waiting for MODE to return {mode}, currently {str(self.mode)}"
                )
                _LOGGER.debug(f"Retry attempt = {retry}")
                asyncio.run(self._writeasync("GET,1:MODE"))
                retry -= 1
            else:
                if retry != 0:
                    _LOGGER.debug(
                        f"MODE confirmed now {str(self.mode)}, proceed to Power On"
                    )
                    self.set_power_on()
                else:
                    _LOGGER.error("Cannot set Intesisbox mode giving up...")

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
    def operation_list(self) -> list[str]:
        """Supported modes."""
        return self._operation_list

    @property
    def vane_horizontal_list(self) -> list[str]:
        """Supported Horizontal Vane settings."""
        return self._horizontal_vane_list

    @property
    def vane_vertical_list(self) -> list[str]:
        """Supported Vertical Vane settings."""
        return self._vertical_vane_list

    @property
    def mode(self) -> str | None:
        """Current mode."""
        return self._device.get(FUNCTION_MODE)

    @property
    def fan_speed(self) -> str | None:
        """Current fan speed."""
        return self._device.get(FUNCTION_FANSP)

    @property
    def fan_speed_list(self) -> list[str]:
        """Supported fan speeds."""
        return self._fan_speed_list

    @property
    def device_mac_address(self) -> str | None:
        """MAC address of the IntesisBox."""
        return self._mac

    @property
    def device_model(self) -> str | None:
        """Model of the IntesisBox."""
        return self._model

    @property
    def firmware_version(self) -> str | None:
        """Firmware versioon of the IntesisBox."""
        return self._firmversion

    @property
    def is_on(self) -> bool:
        """Return true if the controlled device is turned on."""
        return self._device.get(FUNCTION_ONOFF) == POWER_ON

    @property
    def has_swing_control(self) -> bool:
        """Return true if the device supports swing modes."""
        return len(self._horizontal_vane_list) > 1 or len(self._vertical_vane_list) > 1

    @property
    def setpoint(self) -> float | None:
        """Public method returns the target temperature."""
        setpoint = self._device.get(FUNCTION_SETPOINT)
        return (int(setpoint) / 10) if setpoint else None

    @property
    def ambient_temperature(self) -> float | None:
        """Public method returns the current temperature."""
        temperature = self._device.get(FUNCTION_AMBTEMP)
        return (int(temperature) / 10) if temperature else None

    @property
    def max_setpoint(self) -> float | None:
        """Maximum allowed target temperature."""
        return self._setpoint_maximum

    @property
    def min_setpoint(self) -> float | None:
        """Minimum allowed target temperature."""
        return self._setpoint_minimum

    @property
    def rssi(self) -> int | None:
        """Wireless signal strength of the IntesisBox."""
        return self._rssi

    @property
    def vertical_swing(self) -> str | None:
        """Current vertical vane setting."""
        return self._device.get(FUNCTION_VANEUD)

    @property
    def horizontal_swing(self) -> str | None:
        """Current horizontal vane setting."""
        return self._device.get(FUNCTION_VANELR)

    def _send_update_callback(self):
        """Notify all listeners that state of the thermostat has changed."""
        if not self._updateCallbacks:
            _LOGGER.debug("Update callback has not been set by client.")

        for callback in self._updateCallbacks:
            callback()

    def _send_error_callback(self, message: str):
        """Notify all listeners that an error has occurred."""
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
    def error_message(self) -> str | None:
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
