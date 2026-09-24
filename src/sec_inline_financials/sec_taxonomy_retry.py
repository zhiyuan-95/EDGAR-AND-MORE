from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.parse import urlsplit

_RETRY_DELAYS_SECONDS = (1.0, 2.0, 4.0)
_INSTALLATION_MARKER = "_sec_inline_financials_taxonomy_retry_installed"

RetryObserver = Callable[[str, int, float], None]
Sleep = Callable[[float], None]


class WebCache(Protocol):
    retrieve: Callable[..., Any]


def is_sec_extension_taxonomy_schema_url(url: str) -> bool:
    """Return whether URL identifies a filing-specific schema in SEC Archives."""
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    return (
        parsed.scheme.lower() == "https"
        and hostname in {"sec.gov", "www.sec.gov"}
        and path.startswith("/archives/edgar/data/")
        and path.endswith(".xsd")
    )


def install_sec_extension_taxonomy_retry(
    web_cache: WebCache,
    *,
    sleep: Sleep = time.sleep,
    on_retry: RetryObserver | None = None,
) -> None:
    """Add three retries for transient 503s while retrieving SEC filing schemas."""
    if getattr(web_cache, _INSTALLATION_MARKER, False):
        return

    original_retrieve = web_cache.retrieve

    def retrieve(url: str, *args: Any, **kwargs: Any) -> Any:
        retry_number = 0
        while True:
            try:
                return original_retrieve(url, *args, **kwargs)
            except HTTPError as exc:
                if (
                    exc.code != 503
                    or not is_sec_extension_taxonomy_schema_url(url)
                    or retry_number >= len(_RETRY_DELAYS_SECONDS)
                ):
                    raise
                delay = _RETRY_DELAYS_SECONDS[retry_number]
                retry_number += 1
                if on_retry is not None:
                    on_retry(url, retry_number, delay)
                sleep(delay)

    web_cache.retrieve = retrieve
    setattr(web_cache, _INSTALLATION_MARKER, True)
