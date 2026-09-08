"""Tests for the IntesisBox transport layer.

The controller is run against the WMP device emulator in tests/emulator.py,
listening on an ephemeral port on 127.0.0.1, so the tests exercise the real
asyncio protocol rather than a mock.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import importlib.util
from pathlib import Path
from typing import Any

import pytest

from .emulator import start


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
