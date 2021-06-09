"""IntesisBox Climate Platform"""

DOMAIN = "intesisbox"
PLATFORMS = ["climate"]

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Load the saved entities."""
    host = entry.data[CONF_HOST]

    from . import intesisbox
    controller = intesisbox.IntesisBox(host, loop=hass.loop)
    controller.connect()
    while not controller.is_connected:
        await asyncio.sleep(0.1)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = controller

    if entry.unique_id is None:
        hass.config_entries.async_update_entry(entry, unique_id=controller.device_mac_address)

    hass.config_entries.async_setup_platforms(entry, PLATFORMS)

    return True

async def async_unload_entry(hass, entry):
    """Unload a config entry."""
    controller = hass.data[DOMAIN][entry.entry_id]
    controller.stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)