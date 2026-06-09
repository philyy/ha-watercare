"""Watercare buttons."""

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    discovery_info=None,
):
    """Set up the Watercare button platform."""
    async_add_entities([WatercareImportHistoryButton(hass)])


class WatercareImportHistoryButton(ButtonEntity):
    """Button that triggers a full historical statistics import."""

    _attr_has_entity_name = True

    def __init__(self, hass: HomeAssistant):
        """Initialise the button."""
        self._hass = hass
        self._attr_name = "Import history"
        self._attr_unique_id = f"{DOMAIN}_import_history"
        self._attr_icon = "mdi:database-import"

    async def async_press(self):
        """Run the historical import against the configured endpoint."""
        sensor = self._hass.data.get(DOMAIN, {}).get("sensor")
        if sensor is None:
            _LOGGER.error("Watercare sensor not available; cannot import history")
            return
        await sensor.async_import_history()
