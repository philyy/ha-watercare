"""Watercare custom integration."""

from datetime import datetime
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .api import WatercareApi

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.BUTTON]

SERVICE_IMPORT_HISTORY = "import_history"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Watercare from a config entry."""

    # Handle both old (email) and new (username) config formats
    email = entry.data.get("username") or entry.data.get("email")
    password = entry.data.get("password")

    if not email or not password:
        _LOGGER.error("Missing username/email or password in config entry")
        return False

    api = WatercareApi(email, password)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["api"] = api

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _register_import_service(hass)

    # On first setup, backfill historical statistics once in the background.
    if not entry.data.get("history_imported"):

        async def _import_history_once():
            sensor = hass.data.get(DOMAIN, {}).get("sensor")
            if sensor is None:
                return
            await sensor.async_import_history()
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, "history_imported": True}
            )

        entry.async_create_background_task(
            hass, _import_history_once(), "watercare_history_import"
        )

    return True


def _register_import_service(hass: HomeAssistant) -> None:
    """Register the watercare.import_history service once."""
    if hass.services.has_service(DOMAIN, SERVICE_IMPORT_HISTORY):
        return

    async def _handle_import_history(call):
        sensor = hass.data.get(DOMAIN, {}).get("sensor")
        if sensor is None:
            _LOGGER.error("Watercare sensor not available; cannot import history")
            return
        start = None
        start_raw = call.data.get("start_date")
        if start_raw:
            try:
                start = datetime.fromisoformat(start_raw)
            except (ValueError, TypeError):
                _LOGGER.error("Invalid start_date %s; expected ISO format", start_raw)
                return
        await sensor.async_import_history(start=start)

    hass.services.async_register(
        DOMAIN, SERVICE_IMPORT_HISTORY, _handle_import_history
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop("api", None)
        hass.data[DOMAIN].pop("sensor", None)

    return unload_ok
