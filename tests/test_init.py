"""Set up a config entry end-to-end through Home Assistant's loader.

The controller is faked at the single point __init__ creates it; everything
downstream, platform forwarding, runtime_data, the device registry, entity
naming and unload, is Home Assistant's own code running against this
integration's real modules.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.intesisbox import DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, STATE_UNAVAILABLE
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.setup import async_setup_component

MAC = "001DC9A2C911"


class FakeController:
    """Stands in for IntesisBox with a handshake already completed."""

    def __init__(self) -> None:
        self.is_connected = True
        self.device_mac_address = MAC
        self.device_model = "TO-RC-WMP-1"
        self.firmware_version = "v1.3.3"
        self.operation_list = ["AUTO", "HEAT", "DRY", "COOL", "FAN"]
        self.fan_speed_list = ["AUTO", "1", "2", "3"]
        self.vane_vertical_list: list[str] = []
        self.vane_horizontal_list: list[str] = []
        self.has_swing_control = False
        self.min_setpoint = 18.0
        self.max_setpoint = 29.0
        self.mode = "COOL"
        self.fan_speed = "AUTO"
        self.setpoint = 21.0
        self.ambient_temperature = 24.5
        self.vertical_swing = None
        self.horizontal_swing = None
        self.is_on = True
        self.stopped = False
        self._update_callbacks: list = []

    def connect(self) -> None:
        """Already connected."""

    def stop(self) -> None:
        """Record the shutdown."""
        self.stopped = True

    def add_update_callback(self, method) -> None:
        """Record the entity's callback."""
        self._update_callbacks.append(method)


@pytest.fixture(autouse=True)
def _custom_integrations(enable_custom_integrations):
    """Let the harness load custom_components/intesisbox."""


async def _set_up(hass, fake, **entry_kwargs):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="192.0.2.10",
        data={CONF_HOST: "192.0.2.10"},
        **entry_kwargs,
    )
    entry.add_to_hass(hass)
    with patch("custom_components.intesisbox.IntesisBox", return_value=fake):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_entry_stores_the_controller_and_sets_up_the_climate(hass):
    fake = FakeController()
    entry = await _set_up(hass, fake, unique_id=MAC)

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is fake
    assert DOMAIN not in hass.data

    # One device, identified by MAC, and the entity named after it as before.
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, MAC)})
    assert device is not None
    assert device.name == MAC
    assert device.model == "TO-RC-WMP-1"
    assert device.sw_version == "v1.3.3"

    state = hass.states.get("climate.001dc9a2c911")
    assert state is not None
    assert state.attributes["friendly_name"] == MAC
    assert state.state == "cool"
    assert state.attributes["current_temperature"] == 24.5
    assert er.async_get(hass).async_get("climate.001dc9a2c911").device_id == device.id


async def test_an_entry_without_a_unique_id_gets_the_mac(hass):
    fake = FakeController()
    entry = await _set_up(hass, fake)
    assert entry.unique_id == MAC


async def test_unload_stops_the_controller(hass):
    fake = FakeController()
    entry = await _set_up(hass, fake, unique_id=MAC)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert fake.stopped is True
    assert hass.states.get("climate.001dc9a2c911").state == STATE_UNAVAILABLE


async def test_a_failed_platform_unload_keeps_the_controller_running(hass):
    fake = FakeController()
    entry = await _set_up(hass, fake, unique_id=MAC)
    with patch.object(
        hass.config_entries, "async_unload_platforms", new=AsyncMock(return_value=False)
    ):
        assert not await hass.config_entries.async_unload(entry.entry_id)
    assert fake.stopped is False


async def test_a_yaml_platform_keeps_its_configured_name(hass):
    """The YAML path registers no device, so the entity keeps its own name."""
    fake = FakeController()
    with patch("custom_components.intesisbox.intesisbox.IntesisBox", return_value=fake):
        assert await async_setup_component(
            hass,
            "climate",
            {"climate": {"platform": DOMAIN, "host": "192.0.2.10", "name": "Lounge"}},
        )
        await hass.async_block_till_done()

    state = hass.states.get("climate.lounge")
    assert state is not None
    assert state.attributes["friendly_name"] == "Lounge"
