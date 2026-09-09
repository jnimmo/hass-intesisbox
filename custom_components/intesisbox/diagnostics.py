"""Diagnostics download for an IntesisBox config entry."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant

from . import IntesisBoxConfigEntry

# The address and hardware identity of a box on someone's LAN are not needed
# to diagnose a protocol problem, and should not travel with a bug report.
TO_REDACT = {CONF_HOST, "mac", "unique_id"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: IntesisBoxConfigEntry
) -> dict[str, Any]:
    """Return the entry, and the controller's view of the device."""
    # The config flow titles an entry with its host unless the user renames
    # it, so the title would otherwise carry the address the data redacts.
    title = entry.title if entry.title != entry.data.get(CONF_HOST) else REDACTED
    return async_redact_data(
        {
            "entry": {
                "title": title,
                "unique_id": entry.unique_id,
                "data": dict(entry.data),
            },
            "device": entry.runtime_data.diagnostics(),
        },
        TO_REDACT,
    )
