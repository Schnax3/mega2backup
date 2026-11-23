"""Constants for the Mega Backup integration."""

from __future__ import annotations

import logging

from homeassistant.util.hass_dict import HassKey

DOMAIN = "mega2backup"

# Loggers whose level is chosen by the release type (see logs.py).
LOGGER_NAMES = ("custom_components.mega2backup", "mega")
# Full release: errors only. Pre-release: everything.
LOG_LEVEL_RELEASE = logging.ERROR
LOG_LEVEL_PRERELEASE = logging.DEBUG

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

# Chunks of a backup stream are collected up to this size before they are
# written to disk (fewer, larger writes are much faster).
STAGING_BUFFER_SIZE = 8 * 1024 * 1024

# Size of the chunks handed to Home Assistant when a backup is downloaded.
DOWNLOAD_CHUNK_SIZE = 1024 * 1024

DATA_BACKUP_AGENT_LISTENERS: HassKey[list] = HassKey(f"{DOMAIN}.backup_agent_listeners")
