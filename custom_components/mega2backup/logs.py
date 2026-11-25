"""Choose how much this integration writes to the Home Assistant log.

Full releases only log errors, pre-releases (for example ``0.2.0b1`` or
``0.2.0-beta.1`` in manifest.json) log everything. An explicit level set by the
user (``logger:`` in configuration.yaml) always wins.
"""

from __future__ import annotations

import logging
import re

from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import (
    DOMAIN,
    LOG_LEVEL_PRERELEASE,
    LOG_LEVEL_RELEASE,
    LOGGER_NAMES,
)

# Level of the progress messages (upload started, 40 %, finished, ...). The
# "Protokolle" page of Home Assistant only shows WARNING and above, so in a
# pre-release these messages use WARNING to make them visible there. Full
# releases use INFO (and the integration is limited to errors anyway).
_milestone_level = logging.INFO


def milestone_level() -> int:
    """Return the level for progress messages."""
    return _milestone_level


_PRERELEASE_RE = re.compile(r"(?i)[\d.-](?:a|b|rc|alpha|beta|pre|dev)\.?\d*$")


def is_prerelease(version: str | None) -> bool:
    """Return True if the version string marks a pre-release."""
    return bool(version and _PRERELEASE_RE.search(version))


async def async_setup_log_level(hass: HomeAssistant) -> None:
    """Apply the default log level for this version (unless the user set one)."""
    integration = await async_get_integration(hass, DOMAIN)
    version = str(integration.version) if integration.version else None
    global _milestone_level
    level = LOG_LEVEL_PRERELEASE if is_prerelease(version) else LOG_LEVEL_RELEASE
    _milestone_level = logging.WARNING if is_prerelease(version) else logging.INFO

    for name in LOGGER_NAMES:
        logger = logging.getLogger(name)
        if logger.level == logging.NOTSET:
            logger.setLevel(level)

    logging.getLogger(__name__).debug(
        "Version %s is a %s, log level %s",
        version,
        "pre-release" if is_prerelease(version) else "full release",
        logging.getLevelName(level),
    )
