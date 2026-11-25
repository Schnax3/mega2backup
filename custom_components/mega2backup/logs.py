"""Choose how much this integration writes to the Home Assistant log.

Full releases only log errors, pre-releases (for example ``0.2.0b1`` or
``0.2.0-beta.1`` in manifest.json) log everything. An explicit level set by the
user (``logger:`` in configuration.yaml) always wins.
"""

from __future__ import annotations

import logging
import re
import time

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


class TransferLogger:
    """Log speed, percentage and ETA of a transfer at most every few seconds."""

    def __init__(
        self, logger: logging.Logger, label: str, total: int, interval: float = 10.0
    ) -> None:
        """Initialize the logger for a transfer of *total* bytes."""
        self._logger = logger
        self._label = label
        self._total = total
        self._interval = interval
        self._start = time.monotonic()
        self._last_time = self._start
        self._last_bytes = 0

    def update(self, done: int) -> None:
        """Report that *done* bytes are transferred so far."""
        now = time.monotonic()
        if done < self._total and now - self._last_time < self._interval:
            return
        speed = (done - self._last_bytes) / max(now - self._last_time, 1e-6)
        avg = done / max(now - self._start, 1e-6)
        eta = (self._total - done) / avg if avg > 0 else 0
        self._logger.log(
            milestone_level(),
            "%s: %.1f%% (%.0f of %.0f MiB), now %.2f MiB/s, avg %.2f MiB/s, "
            "ETA %d s, elapsed %d s, %s",
            self._label,
            100 * done / self._total if self._total else 100,
            done / 1048576,
            self._total / 1048576,
            speed / 1048576,
            avg / 1048576,
            eta,
            now - self._start,
            memory_summary(),
        )
        self._last_time = now
        self._last_bytes = done


def memory_summary() -> str:
    """Return a short description of free memory and process size (Linux)."""
    try:
        info: dict[str, int] = {}
        with open("/proc/meminfo", encoding="ascii") as handle:
            for line in handle:
                key, _, value = line.partition(":")
                if key in ("MemTotal", "MemAvailable", "SwapFree"):
                    info[key] = int(value.split()[0]) // 1024
        with open("/proc/self/status", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    info["RSS"] = int(line.split()[1]) // 1024
        return (
            f"RAM free {info.get('MemAvailable', '?')}/{info.get('MemTotal', '?')} MiB, "
            f"HA process {info.get('RSS', '?')} MiB, swap free "
            f"{info.get('SwapFree', '?')} MiB"
        )
    except (OSError, ValueError, IndexError):
        return "memory status unavailable"
