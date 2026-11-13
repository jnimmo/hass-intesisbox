"""IntesisBox Climate Platform."""

import asyncio

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

DOMAIN = "intesisbox"
PLATFORMS = ["climate"]

# Upper bound for the TCP connect plus the ID handshake during setup. A device
# that is unreachable (powered off, Wi-Fi down) must not hold setup open: Home
# Assistant would otherwise cancel it and never retry (#79).
SETUP_TIMEOUT = 30


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Load the saved entities."""
    host = entry.data[CONF_HOST]

    from . import intesisbox

    controller = intesisbox.IntesisBox(host, loop=hass.loop)
    controller.connect()
    try:
        async with asyncio.timeout(SETUP_TIMEOUT):
            while not controller.is_connected:
                await asyncio.sleep(0.1)
    except TimeoutError as err:
        # Home Assistant retries the whole setup with its own backoff and a
        # fresh controller, so this one must not keep a connection going.
        controller.stop()
        raise ConfigEntryNotReady(f"IntesisBox at {host} did not answer") from err

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = controller

    if entry.unique_id is None:
        hass.config_entries.async_update_entry(
            entry, unique_id=controller.device_mac_address
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass, entry):
    """Unload a config entry."""
    controller = hass.data[DOMAIN][entry.entry_id]
    controller.stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
