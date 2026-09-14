"""IntesisBox Climate Platform."""

import asyncio

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant

from .const import CONF_POWER_SENSOR, CONF_POWER_THRESHOLD, DEFAULT_POWER_THRESHOLD, DOMAIN  # noqa: F401

PLATFORMS = ["climate"]


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload integration when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


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
        hass.config_entries.async_update_entry(
            entry, unique_id=controller.device_mac_address
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    return True


async def async_unload_entry(hass, entry):
    """Unload a config entry."""
    controller = hass.data[DOMAIN][entry.entry_id]
    controller.stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
