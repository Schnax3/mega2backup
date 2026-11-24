"""Faster uploads to Mega.

async-mega-py uploads a file strictly one chunk after the other and encrypts
every chunk inside the event loop. For multi-gigabyte backups on a small
device that is slow: the CPU work and the network round trips add up, and only
a single connection is used.

This module uploads the same chunks with the same encryption, but

* reads and encrypts the next chunks in a worker thread while earlier chunks
  are still being sent, and
* sends several chunks at the same time over parallel connections (Mega's
  upload servers accept chunks in any order, each chunk carries its offset).

It relies on a few internals of async-mega-py (the version is pinned in
manifest.json). If any of them is missing, or the upload server does not hand
out the completion handle, ``FastUploadUnavailable`` is raised and the caller
falls back to the regular upload.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
import logging
from pathlib import Path
import re
from typing import Any

import aiohttp

from .const import CHUNK_RETRIES, UPLOAD_CONCURRENCY

_LOGGER = logging.getLogger(__name__)

_ERROR_CODE_RE = re.compile(r"^-\d+$")


class FastUploadUnavailable(Exception):
    """The fast upload cannot be used, use the regular one."""


async def _post_chunk(api: Any, url: str, offset: int, data: bytes) -> str:
    """Send one chunk, retrying transient network errors."""
    for attempt in range(1, CHUNK_RETRIES + 1):
        try:
            return (await api.upload_chunk(url, offset, data)).strip()
        except (aiohttp.ClientError, TimeoutError) as err:
            if attempt == CHUNK_RETRIES:
                raise
            _LOGGER.warning(
                "Uploading the chunk at offset %d failed (%s), retry %d/%d",
                offset,
                err or type(err).__name__,
                attempt,
                CHUNK_RETRIES - 1,
            )
            await asyncio.sleep(2 * attempt)
    raise RuntimeError("unreachable")  # pragma: no cover


async def async_fast_upload(
    client: Any,
    path: Path,
    dest_node_id: str,
    on_progress: Callable[[int], None] | None = None,
    concurrency: int = UPLOAD_CONCURRENCY,
) -> Any:
    """Upload *path* into the folder *dest_node_id* and return the new node."""
    file_size = await asyncio.to_thread(lambda: path.stat().st_size)
    handle = await asyncio.to_thread(path.open, "rb")

    async def read_chunk(size: int) -> bytes:
        return await asyncio.to_thread(handle.read, size)

    async def verify_end() -> None:
        if await asyncio.to_thread(handle.read, 1):
            raise ValueError(f"{path.name} changed while it was being uploaded")

    try:
        return await _async_fast_upload(
            client,
            path.name,
            file_size,
            dest_node_id,
            on_progress,
            concurrency,
            read_chunk,
            verify_end,
        )
    finally:
        await asyncio.to_thread(handle.close)


async def async_fast_upload_stream(
    client: Any,
    open_stream: Callable[[], Awaitable[AsyncIterator[bytes]]],
    file_name: str,
    file_size: int,
    dest_node_id: str,
    on_progress: Callable[[int], None] | None = None,
    concurrency: int = UPLOAD_CONCURRENCY,
) -> Any:
    """Upload a byte stream into the folder *dest_node_id* without staging it."""
    stream = await open_stream()
    reader = _AsyncStreamReader(stream, file_size)
    try:
        return await _async_fast_upload(
            client,
            file_name,
            file_size,
            dest_node_id,
            on_progress,
            concurrency,
            reader.read,
            reader.verify_end,
        )
    finally:
        close = getattr(stream, "aclose", None)
        if close is not None:
            await close()


class _AsyncStreamReader:
    """Read exact MEGA chunk sizes from Home Assistant's arbitrary stream chunks."""

    def __init__(self, stream: AsyncIterator[bytes], expected_size: int) -> None:
        self._iterator = stream.__aiter__()
        self._expected_size = expected_size
        self._received = 0
        self._chunks: deque[memoryview] = deque()
        self._buffered = 0
        self._eof = False

    async def _fill(self) -> None:
        while not self._eof:
            try:
                chunk = await anext(self._iterator)
            except StopAsyncIteration:
                self._eof = True
                return
            if not isinstance(chunk, bytes):
                raise TypeError("Home Assistant backup stream yielded non-bytes data")
            self._received += len(chunk)
            if self._received > self._expected_size:
                raise ValueError(
                    "Home Assistant backup stream is larger than its declared size"
                )
            if chunk:
                self._chunks.append(memoryview(chunk))
                self._buffered += len(chunk)
            if chunk:
                return

    async def read(self, size: int) -> bytes:
        parts: list[memoryview] = []
        remaining = size
        while remaining:
            while not self._chunks and not self._eof:
                await self._fill()
            if not self._chunks:
                raise ValueError(
                    "Home Assistant backup stream ended early "
                    f"({self._received} of {self._expected_size} bytes)"
                )
            chunk = self._chunks[0]
            consumed = min(len(chunk), remaining)
            parts.append(chunk[:consumed])
            self._buffered -= consumed
            remaining -= consumed
            if consumed == len(chunk):
                self._chunks.popleft()
            else:
                self._chunks[0] = chunk[consumed:]
        return b"".join(parts)

    async def verify_end(self) -> None:
        while not self._eof:
            await self._fill()
        if self._buffered or self._received != self._expected_size:
            raise ValueError(
                "Home Assistant backup stream size does not match its metadata"
            )


async def _async_fast_upload(
    client: Any,
    file_name: str,
    file_size: int,
    dest_node_id: str,
    on_progress: Callable[[int], None] | None,
    concurrency: int,
    read_chunk: Callable[[int], Awaitable[bytes]],
    verify_end: Callable[[], Awaitable[None]],
) -> Any:
    try:
        from mega import upload as mega_upload  # noqa: PLC0415
        from mega.chunker import MegaChunker, get_chunks  # noqa: PLC0415
        from mega.data_structures import Crypto  # noqa: PLC0415
        from mega.errors import InvalidNameError  # noqa: PLC0415
        from mega.utils import random_u32int_array  # noqa: PLC0415

        core = client._core
        api = core.api
        master_key = core.vault.master_key
        clear_cache = core.clear_cache
        deserialize_node = core._deserialize_node
        request_upload_url = mega_upload._request_upload_url
        finish_upload = mega_upload.finish_upload
    except (ImportError, AttributeError) as err:
        raise FastUploadUnavailable(f"internal API changed: {err!r}") from err

    InvalidNameError.check(file_name)
    if file_size == 0:
        raise FastUploadUnavailable("empty file")

    random_array = random_u32int_array(6)
    chunker = MegaChunker(random_array[:4], random_array[4:])
    upload_url = await request_upload_url(api, file_size)

    if concurrency <= 1:
        sent = 0
        responses: list[str] = []
        for offset, size in get_chunks(file_size):
            raw = await read_chunk(size)
            if len(raw) != size:
                raise ValueError(f"Source ended before MEGA chunk at offset {offset}")
            data = await asyncio.to_thread(chunker.read, raw)
            response = await _post_chunk(api, upload_url, offset, data)
            if response:
                if _ERROR_CODE_RE.match(response):
                    raise RuntimeError(
                        f"Mega rejected the chunk at offset {offset} "
                        f"(error code {response})"
                    )
                responses.append(response)
            sent += len(data)
            if on_progress is not None:
                on_progress(sent)
        await verify_end()
    else:
        responses = []
        await _upload_parallel(
            file_name,
            chunker,
            upload_url,
            file_size,
            api,
            concurrency,
            on_progress,
            responses,
            get_chunks,
            read_chunk,
            verify_end,
        )

    if not responses:
        raise FastUploadUnavailable("the upload server returned no completion handle")
    if len(responses) > 1:
        _LOGGER.debug("Upload returned %d non-empty responses", len(responses))
    file_handle = responses[-1]

    crypto = Crypto.compose(chunker.key, chunker.iv, chunker.compute_meta_mac())
    clear_cache()
    response = await finish_upload(
        api,
        master_key,
        file_handle,
        Path(file_name),
        dest_node_id,
        crypto.full_key,
        crypto.key,
    )
    return deserialize_node(response["f"][0])


async def _upload_parallel(
    file_name: str,
    chunker: Any,
    upload_url: str,
    file_size: int,
    api: Any,
    concurrency: int,
    on_progress: Callable[[int], None] | None,
    responses: list[str],
    get_chunks: Callable[[int], Any],
    read_chunk: Callable[[int], Awaitable[bytes]],
    verify_end: Callable[[], Awaitable[None]],
) -> None:
    """Upload with a bounded in-memory queue for configurable concurrency."""
    queue: asyncio.Queue[tuple[int, bytes] | None] = asyncio.Queue(
        maxsize=concurrency * 2
    )
    sent = 0

    async def producer() -> None:
        for offset, size in get_chunks(file_size):
            raw = await read_chunk(size)
            if len(raw) != size:
                raise ValueError(f"Source ended before MEGA chunk at offset {offset}")
            data = await asyncio.to_thread(chunker.read, raw)
            await queue.put((offset, data))
        await verify_end()
        for _ in range(concurrency):
            await queue.put(None)

    async def worker() -> None:
        nonlocal sent
        while (item := await queue.get()) is not None:
            offset, data = item
            response = await _post_chunk(api, upload_url, offset, data)
            if response:
                if _ERROR_CODE_RE.match(response):
                    raise RuntimeError(
                        f"Mega rejected the chunk at offset {offset} "
                        f"(error code {response})"
                    )
                responses.append(response)
            sent += len(data)
            if on_progress is not None:
                on_progress(sent)

    _LOGGER.debug(
        "Uploading %s (%d bytes) with %d parallel connections",
        file_name,
        file_size,
        concurrency,
    )
    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(producer())
            for _ in range(concurrency):
                group.create_task(worker())
    except BaseExceptionGroup as err:
        # Report the first real error instead of the group wrapper.
        raise err.exceptions[0] from None
