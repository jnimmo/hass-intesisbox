"""Tests for the IntesisBox transport layer.

The controller is run against the WMP device emulator in tests/emulator.py,
listening on an ephemeral port on 127.0.0.1, so the tests exercise the real
asyncio protocol rather than a mock.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import importlib.util
import logging
from pathlib import Path
from typing import Any

import pytest

from .emulator import ID_V6, Emulator, start


@pytest.fixture(autouse=True)
def _real_sockets(socket_enabled):
    """The emulator is a real TCP server on 127.0.0.1.

    The Home Assistant test harness blocks socket construction for every test
    by default; this opts the transport tests back in.
    """


# Load the controller module from its file rather than importing the package,
# so these tests stay independent of Home Assistant's own import machinery.
_SPEC = importlib.util.spec_from_file_location(
    "intesisbox",
    Path(__file__).parent.parent / "custom_components" / "intesisbox" / "intesisbox.py",
)
assert _SPEC is not None and _SPEC.loader is not None
intesisbox = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(intesisbox)


@pytest.fixture
async def server():
    """Run an emulator on an ephemeral port."""
    srv = await start()
    yield srv
    srv.close()
    await srv.wait_closed()


@pytest.fixture
def port(server) -> int:
    """Port the emulator is listening on."""
    return server.sockets[0].getsockname()[1]


async def _wait_until(condition: Callable[[], bool], timeout: float = 15) -> None:
    """Poll a condition until it holds or the timeout expires."""
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.05)


async def _reap_tasks() -> None:
    """Cancel the controller's background tasks and wait for them.

    The periodic tasks sleep until their next tick and only notice a stop
    then, so cancel them outright: the test harness fails any test that leaves
    a task or timer behind.
    """
    tasks = list(intesisbox.background_tasks)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _shutdown(box: Any) -> None:
    """Stop a connected controller and reap its background tasks."""
    box.stop()
    await _reap_tasks()


async def _connect_and_handshake(port: int) -> Any:
    """Connect to the emulator and wait for the handshake to finish."""
    box = intesisbox.IntesisBox("127.0.0.1", port, loop=asyncio.get_running_loop())
    box.connect()
    # LIMITS:VANELR is the last query the controller sends on connect.
    await _wait_until(lambda: bool(box.vane_horizontal_list))
    return box


def _assert_handshake_state(box: Any) -> None:
    """Everything the emulator reports during the handshake, as parsed."""
    assert box.is_connected
    assert box.device_model == "IS-IR-WMP-1"
    assert box.device_mac_address == "001DC9A2C911"
    assert box.firmware_version == "v1.0.2"
    assert box.rssi == "-44"

    assert (box.min_setpoint, box.max_setpoint) == (16.0, 30.0)
    assert box.fan_speed_list == ["AUTO", "1", "2", "3", "4"]
    assert box.operation_list == ["AUTO", "HEAT", "DRY", "COOL", "FAN"]
    assert box.vane_vertical_list == ["AUTO", "1", "2", "3", "SWING"]
    assert box.vane_horizontal_list == ["AUTO", "1", "2", "3", "SWING"]
    assert box.has_swing_control

    # The status dump requested once the ID reply arrived.
    assert box.mode == "AUTO"
    assert box.setpoint == 21.0
    assert box.ambient_temperature == 18.0
    assert not box.is_on


async def test_handshake_reads_identity_limits_and_state(port):
    """Connecting must populate identity, limits and the first status dump."""
    box = await _connect_and_handshake(port)
    try:
        _assert_handshake_state(box)
    finally:
        await _shutdown(box)


@pytest.fixture
async def mode_limits_box():
    """Connect to a device whose limits replies can be held for each test."""
    devices = []

    class ModeLimitsEmulator(Emulator):
        def __init__(self):
            super().__init__()
            self.state["MODE"] = "HEAT"
            self.commands = []
            self.hold_limits = False
            devices.append(self)

        def handle(self, line):
            self.commands.append(line)
            if line == "LIMITS:SETPTEMP":
                if not self.hold_limits:
                    self.send("LIMITS:SETPTEMP,[100,300]\r\n")
            else:
                super().handle(line)

    Emulator.reset()
    server = await asyncio.get_running_loop().create_server(
        ModeLimitsEmulator, "127.0.0.1", 0
    )
    box = None
    try:
        box = await _connect_and_handshake(server.sockets[0].getsockname()[1])
        device = devices[0]
        assert device.commands.count("LIMITS:SETPTEMP") == 1
        device.commands.clear()
        device.hold_limits = True
        updates = []
        box.add_update_callback(lambda: updates.append(box.mode_and_setpoint_limits))
        yield box, device, updates
    finally:
        if box is not None:
            await _shutdown(box)
        server.close()
        await server.wait_closed()


async def test_mode_changes_publish_new_limits_together(mode_limits_box):
    """Request limits once per change and publish mode and limits together."""
    box, device, updates = mode_limits_box
    previous = ("HEAT", 10.0, 30.0)
    for count, (mode, minimum) in enumerate(
        [("COOL", 18.0), ("HEAT", 10.0), ("DRY", 18.0), ("HEAT", 10.0)],
        start=1,
    ):
        updates.clear()
        device.state["MODE"] = mode
        device.send(f"CHN,1:MODE,{mode}\r\nCHN,1:MODE,{mode}\r\n")
        await _wait_until(
            lambda: device.commands.count("LIMITS:SETPTEMP") == count, timeout=1
        )
        assert box.mode == mode  # Internal command confirmation is immediate.
        assert box.mode_and_setpoint_limits == previous  # HA polling stays consistent.
        assert updates == []
        # Interleaved status and an incomplete reply must not release the update.
        device.send(f"CHN,1:AMBTEMP,220\r\nLIMITS:SETPTEMP,[{int(minimum * 10)},")
        await _wait_until(lambda: bool(box._buffer))
        assert updates == []
        assert box.mode_and_setpoint_limits == previous
        device.send("300]\r\n")
        await _wait_until(lambda: bool(updates))
        previous = (mode, minimum, 30.0)
        assert updates == [previous]
        assert device.commands.count("LIMITS:SETPTEMP") == count


async def test_rapid_mode_changes_discard_previous_limits(mode_limits_box):
    """An earlier mode's reply must not be published with a newer mode."""
    box, device, updates = mode_limits_box
    device.send("CHN,1:MODE,COOL\r\nCHN,1:MODE,DRY\r\n")
    await _wait_until(lambda: device.commands.count("LIMITS:SETPTEMP") == 1)
    device.send("LIMITS:SETPTEMP,[180,300]\r\n")
    await _wait_until(lambda: device.commands.count("LIMITS:SETPTEMP") == 2)
    assert updates == []
    assert box.mode_and_setpoint_limits == ("HEAT", 10.0, 30.0)
    device.send("LIMITS:SETPTEMP,[200,280]\r\n")
    await _wait_until(lambda: bool(updates))
    assert updates == [("DRY", 20.0, 28.0)]


@pytest.mark.parametrize("partial_reply", [False, True])
async def test_limits_timeout_and_late_reply(mode_limits_box, caplog, partial_reply):
    """A missing or incomplete reply must time out and recover when it arrives."""
    box, device, updates = mode_limits_box
    device.send("CHN,1:MODE,COOL\r\n")
    if partial_reply:
        device.send("LIMITS:SETPTEMP,[180,")
    await _wait_until(lambda: bool(updates), timeout=1)
    assert updates == [("COOL", 10.0, 30.0)]
    assert "No setpoint limits reply" in caplog.text
    device.send("280]\r\n" if partial_reply else "LIMITS:SETPTEMP,[180,280]\r\n")
    await _wait_until(lambda: len(updates) == 2)
    assert updates[-1] == ("COOL", 18.0, 28.0)


async def test_disconnect_cancels_limits_wait(mode_limits_box, caplog):
    """A pending reply timer must not publish an update after shutdown."""
    box, device, _ = mode_limits_box
    device.send("CHN,1:MODE,COOL\r\n")
    await _wait_until(lambda: "LIMITS:SETPTEMP" in device.commands)
    box.stop()
    await asyncio.sleep(intesisbox.LIMITS_RESPONSE_TIMEOUT + 0.05)
    assert "No setpoint limits reply" not in caplog.text


def test_status_lines_update_state():
    """Several complete CHN lines in one chunk must all be applied."""
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=None)
    box.data_received(b"CHN,1:MODE,HEAT\r\nCHN,1:ONOFF,ON\r\nCHN,1:SETPTEMP,215\r\n")
    assert box.mode == "HEAT"
    assert box.is_on
    assert box.setpoint == 21.5


def test_status_change_notifies_subscribers():
    """A state change must reach every registered update callback."""
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=None)
    calls: list[bool] = []
    box.add_update_callback(lambda: calls.append(True))
    box.data_received(b"CHN,1:AMBTEMP,32768\r\n")  # the device's null value
    assert box.ambient_temperature is None
    assert calls == [True]


async def test_torn_frames_are_reassembled(port):
    """A reply split byte-by-byte must still parse.

    Real devices do this under load. data_received() used to call
    splitlines() on each raw chunk, so a frame cut mid-line raised IndexError
    inside the protocol callback and asyncio closed the connection.
    """
    Emulator.tear_frames = True
    box = await _connect_and_handshake(port)
    try:
        _assert_handshake_state(box)
    finally:
        await _shutdown(box)


def test_partial_line_is_held_until_the_rest_arrives():
    """Nothing is parsed from a fragment; the whole line is, once complete."""
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=None)
    box.data_received(b"CHN,1:AMBTE")
    assert box.ambient_temperature is None
    box.data_received(b"MP,220\r\n")
    assert box.ambient_temperature == 22.0


def test_line_endings_are_all_accepted():
    """The spec allows \\r, \\n or \\r\\n; a \\r\\n split across chunks is one end."""
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=None)
    box.data_received(b"CHN,1:MODE,HEAT\rCHN,1:FANSP,2\nCHN,1:ONOFF,ON\r")
    box.data_received(b"\nCHN,1:SETPTEMP,215\r\n")
    assert box.mode == "HEAT"
    assert box.fan_speed == "2"
    assert box.is_on
    assert box.setpoint == 21.5


def test_runaway_line_is_discarded(caplog):
    """Bytes that never see a line ending must not accumulate forever."""
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=None)
    with caplog.at_level(logging.WARNING):
        box.data_received(b"x" * (intesisbox.MAX_LINE_LENGTH + 1))
    assert box._buffer == b""
    assert "no line ending" in caplog.text
    # The stream is usable again afterwards.
    box.data_received(b"CHN,1:MODE,DRY\r\n")
    assert box.mode == "DRY"


def test_malformed_lines_are_skipped_not_fatal(caplog):
    """A garbage line must be logged and skipped, not kill the connection."""
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=None)
    pushes: list[bool] = []
    box.add_update_callback(lambda: pushes.append(True))
    with caplog.at_level(logging.WARNING):
        box.data_received(
            b"\r\n\r\n"  # blank lines
            b"CHN,1:NOCOMMA\r\n"  # no value
            b"LIMITS:\r\n"  # nothing after the colon
            b"LIMITS:SETPTEMP,[a,b]\r\n"  # non-numeric limits
            b"CHN,1:MODE,H\xc3\xa9AT\r\n"  # non-ASCII
            b"HELLO\r\n"  # no colon at all
            b"CHN,1:MODE,COOL\r\n"
        )
    assert box.mode == "COOL"
    assert box.min_setpoint is None
    # One real change in that chunk, so exactly one notification.
    assert pushes == [True]
    assert "Malformed change message" in caplog.text
    assert "Non-numeric setpoint limits" in caplog.text
    assert "non-ASCII" in caplog.text


def test_a_parser_exception_does_not_kill_the_socket(caplog):
    """An unexpected failure on one line must not take the connection down."""
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=None)
    box._parse_change_received = lambda args: 1 / 0  # type: ignore[method-assign]
    with caplog.at_level(logging.ERROR):
        box.data_received(b"CHN,1:MODE,HEAT\r\nCHN,1:ONOFF,ON\r\n")
    assert "Failed to process line" in caplog.text


async def test_v6_id_banner_field_offsets():
    """V6 gateways omit the Protocol field, shifting version and RSSI left."""
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=asyncio.get_running_loop())
    try:
        box.data_received(f"{ID_V6}\r\n".encode())
        assert box.device_model == "INWMPUNI001I000"
        assert box.device_mac_address == "001DC9A2C911"
        assert box.firmware_version == "v1.0.1"
        assert box.rssi == "-44"
        assert box.is_connected
    finally:
        # Never connected a socket, so there is nothing to stop; only the
        # pollers the ID reply started.
        await _reap_tasks()


def test_short_id_reply_does_not_count_as_connected(caplog):
    """A reply with no identity in it is logged, and the handshake goes on."""
    box = intesisbox.IntesisBox("127.0.0.1", 1, loop=None)
    with caplog.at_level(logging.WARNING):
        box.data_received(b"ID:too,short\r\n")
    assert "Unexpected ID reply" in caplog.text
    assert box.device_mac_address is None
    assert not box.is_connected
    assert not intesisbox.background_tasks
