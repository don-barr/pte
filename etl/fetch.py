"""Make sure every in-network file an index references is present locally."""

import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx

from etl.index import in_network_urls
from etl.schema import local_path_for

CHUNK_SIZE = 1024 * 1024
TIMEOUT = httpx.Timeout(60.0, connect=15.0)


# Called as a download goes: (bytes received so far, total from Content-Length or None) (#42.2).
OnBytes = Callable[[int, "int | None"], None]


class IncompleteDownload(Exception):
    """The body had fewer (or more) bytes than the response's Content-Length declared."""

    def __init__(self, received: int, declared: int):
        super().__init__(f"download incomplete: got {received:,} of {declared:,} bytes")


@dataclass
class FetchResult:
    url: str
    path: Path | None
    downloaded: bool
    error: str | None


def fetch_all(index: dict, raw_dir: Path, download: bool = True) -> list[FetchResult]:
    """One result per in-network URL in index order; failures are reported, never raised."""
    return fetch_urls(in_network_urls(index), raw_dir, download)


def fetch_urls(urls: list[str], raw_dir: Path, download: bool = True) -> list[FetchResult]:
    """One result per URL, in order; a file already in raw_dir is not downloaded again."""
    raw_dir = Path(raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    results = []
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
        for url in urls:
            path = local_path_for(url, raw_dir)
            if path.exists():
                results.append(FetchResult(url, path, downloaded=False, error=None))
            elif not download:
                results.append(FetchResult(url, None, False, "not downloaded (download disabled)"))
            else:
                results.append(_download(client, url, path))
    return results


def download_url(url: str, raw_dir: Path, on_bytes: OnBytes | None = None) -> FetchResult:
    """Download url into raw_dir even if a copy is already there, replacing it atomically."""
    raw_dir = Path(raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
        return _download(client, url, local_path_for(url, raw_dir), on_bytes)


def fetch_bytes(url: str, on_bytes: OnBytes | None = None) -> tuple[bytes | None, str | None]:
    """GET url into memory: (content, None), or (None, error). Nothing is written to disk."""
    try:
        with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                with _complete(response):
                    chunks = list(_chunks(response, on_bytes))
            return b"".join(chunks), None
    except IncompleteDownload as exc:
        return None, str(exc)
    except httpx.HTTPError as exc:
        return None, f"download failed: {_describe(exc)}"


def _download(client: httpx.Client, url: str, path: Path, on_bytes: OnBytes | None = None) -> FetchResult:
    """Stream url into a temp file beside path, then rename it into place."""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".part")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as out:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                with _complete(response):
                    for chunk in _chunks(response, on_bytes):
                        out.write(chunk)
        tmp.replace(path)
    except IncompleteDownload as exc:
        tmp.unlink(missing_ok=True)
        return FetchResult(url, None, False, str(exc))
    except (httpx.HTTPError, OSError) as exc:
        tmp.unlink(missing_ok=True)
        return FetchResult(url, None, False, f"download failed: {_describe(exc)}")
    return FetchResult(url, path, downloaded=True, error=None)


def _chunks(response: httpx.Response, on_bytes: OnBytes | None):
    """response's body in chunks, telling on_bytes how much has arrived (first 0) as they come."""
    declared = response.headers.get("Content-Length")
    total = int(declared) if declared and declared.isdigit() else None
    received = 0
    if on_bytes:
        on_bytes(0, total)
    for chunk in response.iter_bytes(CHUNK_SIZE):
        received += len(chunk)
        if on_bytes:
            on_bytes(received, total)
        yield chunk


@contextmanager
def _complete(response: httpx.Response):
    """Around reading response's body: raise IncompleteDownload unless the raw bytes received
    (before any Content-Encoding is undone) match a declared Content-Length, including when
    the connection drops partway through."""
    declared = response.headers.get("Content-Length")
    declared = int(declared) if declared and declared.isdigit() else None
    try:
        yield
    except httpx.TransportError:
        if declared is not None and response.num_bytes_downloaded != declared:
            raise IncompleteDownload(response.num_bytes_downloaded, declared) from None
        raise
    if declared is not None and response.num_bytes_downloaded != declared:
        raise IncompleteDownload(response.num_bytes_downloaded, declared)


def _describe(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code} {exc.response.reason_phrase}"
    return f"{type(exc).__name__}: {exc}"
