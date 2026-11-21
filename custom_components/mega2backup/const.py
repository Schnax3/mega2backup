"""Constants for the Mega Backup integration."""

from __future__ import annotations

from homeassistant.util.hass_dict import HassKey

DOMAIN = "mega2backup"

CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_FOLDER = "folder"

DEFAULT_FOLDER = "Home Assistant Backups"

# Characters Mega does not allow in node names.
INVALID_FOLDER_CHARS = '"*/:<>?\\|'

TAR_SUFFIX = ".tar"
METADATA_SUFFIX = ".metadata.json"

# How long the folder listing is cached (seconds).
LISTING_CACHE_TTL = 300

# Size of the chunks handed to Home Assistant when a backup is downloaded.
DOWNLOAD_CHUNK_SIZE = 1024 * 1024

DATA_BACKUP_AGENT_LISTENERS: HassKey[list] = HassKey(f"{DOMAIN}.backup_agent_listeners")
