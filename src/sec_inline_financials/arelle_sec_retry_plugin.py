from __future__ import annotations

import logging
from typing import Any

from sec_inline_financials.sec_taxonomy_retry import (
    install_sec_extension_taxonomy_retry,
)

_PLUGIN_NAME = "SEC extension taxonomy retry"


def _install_retry(cntlr: Any, options: Any, *args: Any, **kwargs: Any) -> None:
    del options, args, kwargs

    def log_retry(url: str, retry_number: int, delay: float) -> None:
        cntlr.addToLog(
            (
                "SEC returned Service Unavailable for a filing extension taxonomy schema; "
                "retry %(retryNumber)s of 3 in %(delay)s seconds."
            ),
            messageCode="secInlineFinancials:taxonomyRetry",
            messageArgs={"retryNumber": retry_number, "delay": delay},
            file=url,
            level=logging.WARNING,
        )

    install_sec_extension_taxonomy_retry(cntlr.webCache, on_retry=log_retry)


__pluginInfo__ = {
    "name": _PLUGIN_NAME,
    "version": "1.0.0",
    "description": "Retry transient SEC extension taxonomy schema retrieval failures.",
    "license": "Apache-2",
    "author": "SEC Inline Financials",
    "CntlrCmdLine.Utility.Run": _install_retry,
}
