"""Thin async wrapper around async-mega-py used to store Home Assistant backups."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Generator
import contextlib
import json
import logging
from pathlib import Path
import shutil
import tempfile
import time
from typing import Any, TypeVar

import aiohttp
from mega.client import MegaNzClient
from mega.data_structures import Node
from mega.errors import MegaNzError, RequestError, ValidationError

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .const import FAST_UPLOAD_MIN_SIZE, INVALID_FOLDER_CHARS, LISTING_CACHE_TTL
from .fast_upload import FastUploadUnavailable, async_fast_upload

_LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")

# Mega API error codes (see mega.errors)
_ERR_NOT_FOUND = -9
_ERR_BLOCKED = -16
_ERR_SESSION = -15
_ERR_MFA_REQUIRED = -26

ProgressCallback = Callable[[int], None]

MIB = 1024 * 1024
GIB = 1024 * MIB


def _read_system_memory() -> tuple[int, int] | None:
    """Return total and available physical memory in bytes."""
    try:
        values: dict[str, int] = {}
        with open("/proc/meminfo", encoding="ascii") as meminfo:
            for line in meminfo:
                key, separator, value = line.partition(":")
                if separator and key in ("MemTotal", "MemAvailable"):
                    fields = value.split()
                    if fields:
                        values[key] = int(fields[0]) * 1024
        if "MemTotal" in values and "MemAvailable" in values:
            return values["MemTotal"], values["MemAvailable"]
    except (OSError, ValueError):
        pass
    return None


def _select_upload_concurrency(total: int | None, available: int | None) -> int:
    """Choose upload concurrency based on installed and currently free RAM."""
    if total is None or available is None:
        return 1
    if total >= 8 * GIB and available >= 2 * GIB:
        return 4
    if total >= 4 * GIB and available >= 1536 * MIB:
        return 2
    if total >= 2 * GIB and available >= GIB:
        return 2
    return 1


class MegaError(Exception):
    """Base error of the Mega backup client."""


class MegaAuthError(MegaError):
    """Login failed (wrong credentials or blocked account)."""


class MegaMfaRequiredError(MegaAuthError):
    """The account requires multi-factor authentication."""


class MegaSessionExpiredError(MegaError):
    """The Mega session is no longer valid."""


class MegaConnectionError(MegaError):
    """Mega could not be reached."""


class MegaApiError(MegaError):
    """Mega returned an error."""


class MegaInvalidFolderError(MegaError):
    """The configured folder name is not usable."""


def validate_folder_name(folder: str) -> str:
    """Return the cleaned folder name or raise MegaInvalidFolderError."""
    folder = folder.strip()
    if not folder or any(char in folder for char in INVALID_FOLDER_CHARS):
        raise MegaInvalidFolderError(folder)
    return folder


@contextlib.contextmanager
def _translate_errors(*, login: bool = False) -> Generator[None]:
    """Translate library / network errors into MegaError subclasses.

    Every error is written to the Home Assistant log.
    """
    try:
        yield
    except MegaError:
        raise  # already translated and logged
    except RequestError as err:
        code = err.code
        if login and code == _ERR_MFA_REQUIRED:
            _LOGGER.error("Mega login needs two-factor authentication (not supported)")
            raise MegaMfaRequiredError(str(err)) from err
        if login and code in (_ERR_NOT_FOUND, _ERR_BLOCKED):
            _LOGGER.error("Mega login rejected (code %s): %s", code, err)
            raise MegaAuthError(str(err)) from err
        if code == _ERR_SESSION:
            _LOGGER.info("Mega session is no longer valid (code %s)", code)
            raise MegaSessionExpiredError(str(err)) from err
        _LOGGER.error("Mega API error (code %s): %s", code, err)
        raise MegaApiError(str(err)) from err
    except (aiohttp.ClientError, TimeoutError) as err:
        _LOGGER.error(
            "Connection to Mega failed (%s): %s",
            type(err).__name__,
            err or "no details",
        )
        raise MegaConnectionError(str(err) or type(err).__name__) from err
    except (MegaNzError, ValidationError, LookupError, RuntimeError) as err:
        _LOGGER.error("Mega client error (%s): %s", type(err).__name__, err)
        raise MegaApiError(str(err) or type(err).__name__) from err
    except Exception as err:
        _LOGGER.exception("Unexpected error in the Mega client: %s", err)
        raise MegaApiError(f"Unexpected error: {err}") from err


@contextlib.contextmanager
def _upload_progress(callback: ProgressCallback | None) -> Generator[None]:
    """Route async-mega-py upload progress to *callback* (best effort).

    The library has no public progress callback, but exposes a context variable
    holding a hook factory. If that internal API ever changes, uploads simply
    continue without progress reporting.
    """
    if callback is None:
        yield
        return

    try:
        from mega import progress as mega_progress  # noqa: PLC0415

        factory_var = mega_progress._PROGRESS_HOOK_FACTORY
    except (ImportError, AttributeError):
        yield
        return

    sent = 0

    @contextlib.contextmanager
    def factory(description: str, total: float, kind: str) -> Generator[Any]:
        def hook(advance: float) -> None:
            nonlocal sent
            if kind == "UP":
                sent += int(advance)
                callback(sent)

        yield hook

    token = factory_var.set(factory)
    try:
        yield
    finally:
        factory_var.reset(token)


class _TempfileShim:
    """Make async-mega-py create its download temp files in our staging dir.

    The library downloads into the system temp directory first, which is a
    small RAM disk on Home Assistant OS. The shim is only installed in the
    library's own module, the global ``tempfile`` module stays untouched.
    """

    def __init__(self, base: Path) -> None:
        self._base = base

    def __getattr__(self, name: str) -> Any:
        return getattr(tempfile, name)

    def NamedTemporaryFile(self, *args: Any, **kwargs: Any) -> Any:
        """Create the temporary file inside the staging directory."""
        self._base.mkdir(parents=True, exist_ok=True)
        kwargs.setdefault("dir", str(self._base))
        return tempfile.NamedTemporaryFile(*args, **kwargs)


def _redirect_library_temp_files(base: Path) -> None:
    try:
        from mega import download as mega_download  # noqa: PLC0415

        mega_download.tempfile = _TempfileShim(base)  # type: ignore[attr-defined]
    except (ImportError, AttributeError):
        _LOGGER.debug("Could not redirect async-mega-py temp files")


class MegaBackupClient:
    """Stores and retrieves backup files in one folder of a Mega account."""

    def __init__(
        self,
        hass: HomeAssistant,
        email: str,
        password: str,
        folder: str,
    ) -> None:
        """Initialize the client (no network access yet)."""
        self._hass = hass
        self._email = email
        self._password = password
        self._folder = validate_folder_name(folder)
        _redirect_library_temp_files(_temp_base(hass))
        # Transfers can take a long time, so no total timeout.
        self._session = async_create_clientsession(
            hass,
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=120),
        )
        self._client: MegaNzClient | None = None
        self._lock = asyncio.Lock()
        self._files_cache: tuple[float, dict[str, Node]] | None = None
        self._json_cache: dict[str, dict[str, Any]] = {}
        self._fast_upload = True

    @property
    def email(self) -> str:
        """Return the account e-mail address."""
        return self._email

    @property
    def folder(self) -> str:
        """Return the configured backup folder name."""
        return self._folder

    # ------------------------------------------------------------------ session

    async def async_connect(self) -> None:
        """Log in and make sure the backup folder exists."""
        _redirect_library_tempdir(_temp_base(self._hass))
        async with self._lock:
            await self._async_login()
        await self._call(self._async_ensure_folder)

    async def async_close(self) -> None:
        """Close the HTTP session."""
        self._client = None
        await self._session.close()

    async def _async_login(self) -> None:
        client = MegaNzClient(self._session)
        with _translate_errors(login=True):
            await client.login(self._email, self._password)
        self._client = client
        self._files_cache = None

    async def _call(self, func: Callable[[MegaNzClient], Awaitable[_T]]) -> _T:
        """Run *func* with a logged-in client, re-logging in once if needed."""
        for attempt in (1, 2):
            async with self._lock:
                if self._client is None:
                    await self._async_login()
                client = self._client
            assert client is not None
            try:
                with _translate_errors():
                    return await func(client)
            except MegaSessionExpiredError as err:
                if attempt == 2:
                    raise MegaAuthError(str(err)) from err
                _LOGGER.info("Mega session expired, logging in again")
                async with self._lock:
                    self._client = None
        raise MegaApiError("unreachable")  # pragma: no cover

    async def _async_ensure_folder(self, client: MegaNzClient) -> Node:
        fs = await client.get_filesystem(force=True)
        try:
            node = fs.find(self._folder)
        except FileNotFoundError:
            _LOGGER.info("Creating Mega backup folder '%s'", self._folder)
            return await client.create_folder(self._folder)
        if not node.is_folder:
            raise MegaApiError(f"'{self._folder}' exists but is not a folder")
        return node

    # ------------------------------------------------------------------ listing

    async def async_list_files(self, *, force: bool = False) -> dict[str, Node]:
        """Return the files in the backup folder, keyed by file name."""
        cached = self._files_cache
        if (
            not force
            and cached is not None
            and time.monotonic() - cached[0] < LISTING_CACHE_TTL
        ):
            return cached[1]

        async def _list(client: MegaNzClient) -> dict[str, Node]:
            folder = await self._async_ensure_folder(client)
            fs = await client.get_filesystem()
            return {
                node.attributes.name: node
                for node in fs.iterdir(folder.id)
                if node.is_file
            }

        files = await self._call(_list)
        self._files_cache = (time.monotonic(), files)
        return files

    def invalidate_cache(self) -> None:
        """Forget the cached folder listing."""
        self._files_cache = None

    # ---------------------------------------------------------------- transfers

    async def async_download(self, node: Node, target_dir: Path) -> Path:
        """Download *node* into *target_dir* and return the local path."""

        async def _download(client: MegaNzClient) -> Path:
            return await client.download(node, target_dir)

        return await self._call(_download)

    async def async_read_json(self, node: Node) -> dict[str, Any]:
        """Download a small JSON file and return its parsed content."""
        if (cached := self._json_cache.get(node.id)) is not None:
            return cached

        tmp_dir = await async_make_temp_dir(self._hass)
        try:
            path = await self.async_download(node, tmp_dir)
            text = await self._hass.async_add_executor_job(path.read_text, "utf-8")
        finally:
            await async_remove_dir(self._hass, tmp_dir)

        try:
            data = json.loads(text)
        except ValueError as err:
            raise MegaApiError(f"'{node.attributes.name}' is not valid JSON") from err
        if not isinstance(data, dict):
            raise MegaApiError(f"'{node.attributes.name}' has an unexpected format")
        self._json_cache[node.id] = data
        return data

    async def async_upload(
        self,
        path: Path,
        on_progress: ProgressCallback | None = None,
    ) -> Node:
        """Upload the local file *path* into the backup folder."""

        size = await self._hass.async_add_executor_job(lambda: path.stat().st_size)

        async def _upload(client: MegaNzClient) -> Node:
            folder = await self._async_ensure_folder(client)
            if self._fast_upload and size >= FAST_UPLOAD_MIN_SIZE:
                memory = await self._hass.async_add_executor_job(_read_system_memory)
                if memory is None:
                    concurrency = 1
                    _LOGGER.info(
                        "Mega upload concurrency: 1 connection "
                        "(system memory status unavailable)"
                    )
                else:
                    total, available = memory
                    concurrency = _select_upload_concurrency(total, available)
                    _LOGGER.info(
                        "Mega upload concurrency: %d connection%s "
                        "(%d MiB available of %d MiB total)",
                        concurrency,
                        "" if concurrency == 1 else "s",
                        available // MIB,
                        total // MIB,
                    )
                try:
                    return await async_fast_upload(
                        client,
                        path,
                        folder.id,
                        on_progress,
                        concurrency,
                    )
                except FastUploadUnavailable as err:
                    _LOGGER.warning(
                        "Parallel upload not possible (%s), using the standard "
                        "upload instead",
                        err,
                    )
                    self._fast_upload = False
            with _upload_progress(on_progress):
                return await client.upload(path, folder.id)

        try:
            return await self._call(_upload)
        finally:
            self.invalidate_cache()

    async def async_delete(self, *nodes: Node) -> None:
        """Permanently delete *nodes* (bypassing the Mega rubbish bin)."""

        async def _delete(client: MegaNzClient) -> None:
            for node in nodes:
                await client.destroy(node.id)

        try:
            await self._call(_delete)
        finally:
            self.invalidate_cache()
            for node in nodes:
                self._json_cache.pop(node.id, None)


class _TempfileShim:
    """Stand-in for ``tempfile`` inside ``mega.download``.

    async-mega-py writes every download to a temporary file in the system temp
    directory before moving it. On Home Assistant OS that directory is a small
    RAM disk, so large backups would fill it. The shim puts those files into
    our own staging directory on the data partition instead.
    """

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def NamedTemporaryFile(self, *args: Any, **kwargs: Any) -> Any:
        """Create the temporary file in the staging directory."""
        kwargs.setdefault("dir", self._directory)
        return tempfile.NamedTemporaryFile(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(tempfile, name)


def _redirect_library_tempdir(directory: Path) -> None:
    """Make async-mega-py create its download temp files in *directory*."""
    try:
        from mega import download as mega_download  # noqa: PLC0415
    except ImportError:
        return
    if hasattr(mega_download, "tempfile"):
        mega_download.tempfile = _TempfileShim(directory)
    else:  # pragma: no cover
        _LOGGER.debug("async-mega-py no longer uses tempfile, nothing to redirect")


def _temp_base(hass: HomeAssistant) -> Path:
    """Return the staging directory.

    It lives inside the Home Assistant config directory (the data partition)
    because /tmp is often a small RAM disk, which is too small for backups.
    """
    return Path(hass.config.path("mega2backup_tmp"))


def _make_temp_dir(base: Path) -> str:
    base.mkdir(parents=True, exist_ok=True)
    return tempfile.mkdtemp(prefix="job_", dir=base)


def _remove_dir(path: Path | str) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _free_space(base: Path) -> int:
    base.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(base).free


async def async_make_temp_dir(hass: HomeAssistant) -> Path:
    """Create a temporary working directory."""
    base = _temp_base(hass)
    return Path(await hass.async_add_executor_job(_make_temp_dir, base))


async def async_remove_dir(hass: HomeAssistant, path: Path) -> None:
    """Remove a temporary working directory."""
    await hass.async_add_executor_job(_remove_dir, path)


async def async_cleanup_temp(hass: HomeAssistant) -> None:
    """Remove leftovers of interrupted transfers (call when no transfer runs)."""
    await hass.async_add_executor_job(_remove_dir, _temp_base(hass))


async def async_free_space(hass: HomeAssistant) -> int:
    """Return the free bytes on the staging partition."""
    return await hass.async_add_executor_job(_free_space, _temp_base(hass))
