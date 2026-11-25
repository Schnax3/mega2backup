"""The Mega Backup integration: use Mega.io as a Home Assistant backup location."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .api import MegaAuthError, MegaBackupClient, MegaError, async_cleanup_temp
from .const import (
    CONF_EMAIL,
    CONF_FOLDER,
    CONF_PASSWORD,
    DATA_BACKUP_AGENT_LISTENERS,
    DEFAULT_FOLDER,
)
from .logs import async_setup_log_level, milestone_level

_LOGGER = logging.getLogger(__name__)

type MegaConfigEntry = ConfigEntry[MegaBackupClient]


def _mask_email(email: str) -> str:
    """Hide most of the local part so logs can be shared safely."""
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}"


async def async_setup_entry(hass: HomeAssistant, entry: MegaConfigEntry) -> bool:
    """Set up Mega Backup from a config entry."""
    await async_setup_log_level(hass)
    await async_cleanup_temp(hass)
    client = MegaBackupClient(
        hass,
        entry.data[CONF_EMAIL],
        entry.data[CONF_PASSWORD],
        entry.data.get(CONF_FOLDER, DEFAULT_FOLDER),
    )

    try:
        await client.async_connect()
    except MegaAuthError as err:
        await client.async_close()
        _LOGGER.error("Mega login failed, please re-authenticate: %s", err)
        raise ConfigEntryAuthFailed(
            f"Login to Mega failed for {entry.data[CONF_EMAIL]}: {err}"
        ) from err
    except MegaError as err:
        await client.async_close()
        _LOGGER.warning("Could not reach Mega, will retry: %s", err)
        raise ConfigEntryNotReady(f"Could not connect to Mega: {err}") from err

    entry.runtime_data = client
    _LOGGER.log(
        milestone_level(),
        "Connected to Mega account %s, backups go to folder '%s'",
        _mask_email(entry.data[CONF_EMAIL]),
        client.folder,
    )

    @callback
    def _async_notify_backup_listeners() -> None:
        for listener in hass.data.get(DATA_BACKUP_AGENT_LISTENERS, []):
            listener()

    # Tell the backup integration to (re)load its agents whenever this entry
    # is loaded or unloaded.
    entry.async_on_unload(entry.async_on_state_change(_async_notify_backup_listeners))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: MegaConfigEntry) -> bool:
    """Unload a config entry."""
    await entry.runtime_data.async_close()
    return True
