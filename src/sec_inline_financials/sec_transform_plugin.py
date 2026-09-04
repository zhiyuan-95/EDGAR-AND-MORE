from __future__ import annotations

import hashlib
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sec_inline_financials.errors import ProcessingError

_EDGAR_COMMIT = "72033f579e89ab47e882437b5d4ceed9c7656ed5"
_RAW_ROOT = "https://raw.githubusercontent.com/Arelle/EDGAR"


@dataclass(frozen=True)
class PluginFile:
    name: str
    sha256: str


_MANIFEST = (
    PluginFile(
        name="__init__.py",
        sha256="2296586f945ffd95ab37d3d4147c4e45f42ce4e5f397f61143e053208adefbf1",
    ),
    PluginFile(
        name="text2num.py",
        sha256="6c9b26354320a2fb34fe380cdc71ff7f8d1cc712811e6b10c661e512f0383409",
    ),
)

FetchBytes = Callable[[str], bytes]


def _fetch_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "sec-inline-financials/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return bytes(response.read())
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        raise ProcessingError(
            f"Could not download the official SEC transform plugin: {exc}"
        ) from exc


def _matches(path: Path, expected_sha256: str) -> bool:
    if not path.is_file():
        return False
    try:
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return False
    return actual == expected_sha256


def ensure_sec_transform_plugin(
    cache_root: Path,
    *,
    fetch_bytes: FetchBytes = _fetch_bytes,
    manifest: tuple[PluginFile, ...] = _MANIFEST,
    commit: str = _EDGAR_COMMIT,
) -> Path:
    """Install the pinned official SEC transform plugin into a verified local cache."""
    plugin_dir = cache_root / f"sec-transform-{commit[:12]}"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    for item in manifest:
        destination = plugin_dir / item.name
        if _matches(destination, item.sha256):
            continue
        url = f"{_RAW_ROOT}/{commit}/transform/{item.name}"
        content = fetch_bytes(url)
        actual = hashlib.sha256(content).hexdigest()
        if actual != item.sha256:
            raise ProcessingError(
                f"Checksum verification failed for official SEC transform plugin file {item.name}."
            )
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        try:
            temporary.write_bytes(content)
            temporary.replace(destination)
        except OSError as exc:
            raise ProcessingError(
                f"Could not cache SEC transform plugin file {destination}: {exc}"
            ) from exc
    return plugin_dir
