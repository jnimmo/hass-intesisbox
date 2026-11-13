"""Config entry setup, through Home Assistant's own loader."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.intesisbox import DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST


class SilentController:
    """A device that never answers ID."""

    def __init__(self, *args, **kwargs) -> None:
        self.is_connected = False
        self.stopped = False

    def connect(self) -> None:
        """Start an attempt that never completes."""

    def stop(self) -> None:
        """Record the shutdown."""
        self.stopped = True


@pytest.fixture(autouse=True)
def _custom_integrations(enable_custom_integrations):
    """Let the harness load custom_components/intesisbox."""


async def test_an_unreachable_device_retries_setup_instead_of_hanging(hass):
    """Setup gives up on a silent device and lets Home Assistant retry (#79)."""
    controller = SilentController()
    entry = MockConfigEntry(
        domain=DOMAIN, title="192.0.2.10", data={CONF_HOST: "192.0.2.10"}
    )
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.intesisbox.intesisbox.IntesisBox",
            return_value=controller,
        ),
        patch("custom_components.intesisbox.SETUP_TIMEOUT", 0.3),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert controller.stopped is True
    assert not hass.states.async_entity_ids("climate")
