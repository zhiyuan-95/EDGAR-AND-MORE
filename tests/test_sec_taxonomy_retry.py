from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path
from urllib.error import HTTPError

import pytest

import sec_inline_financials.arelle_adapter as arelle_adapter
from sec_inline_financials.arelle_adapter import ArelleProcessor
from sec_inline_financials.errors import ProcessingError
from sec_inline_financials.models import Company, Filing
from sec_inline_financials.sec_taxonomy_retry import (
    install_sec_extension_taxonomy_retry,
)

GLD_SCHEMA_URL = (
    "https://www.sec.gov/Archives/edgar/data/1222333/000143774924036161/gld-20240930.xsd"
)


class FakeWebCache:
    def __init__(self, outcomes: list[object]) -> None:
        self._outcomes = iter(outcomes)
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        self.retrieve: Callable[..., object] = self._retrieve

    def _retrieve(self, url: str, *args: object, **kwargs: object) -> object:
        self.calls.append((url, args, kwargs))
        outcome = next(self._outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def service_unavailable(url: str = GLD_SCHEMA_URL) -> HTTPError:
    return HTTPError(url, 503, "Service Unavailable", hdrs=None, fp=None)


def install_retry(
    web_cache: FakeWebCache,
    sleeps: list[float],
) -> None:
    install_sec_extension_taxonomy_retry(web_cache, sleep=sleeps.append)


def test_retries_sec_extension_schema_three_times_after_503() -> None:
    expected = ("schema.xsd", {"content-type": "application/xml"}, b"<schema />")
    web_cache = FakeWebCache(
        [service_unavailable(), service_unavailable(), service_unavailable(), expected]
    )
    sleeps: list[float] = []
    install_retry(web_cache, sleeps)

    result = web_cache.retrieve(GLD_SCHEMA_URL, filename="schema.xsd")

    assert result == expected
    assert len(web_cache.calls) == 4
    assert all(call == (GLD_SCHEMA_URL, (), {"filename": "schema.xsd"}) for call in web_cache.calls)
    assert sleeps == [1.0, 2.0, 4.0]


def test_raises_after_three_additional_attempts() -> None:
    web_cache = FakeWebCache([service_unavailable() for _ in range(4)])
    sleeps: list[float] = []
    install_retry(web_cache, sleeps)

    with pytest.raises(HTTPError, match="Service Unavailable") as error:
        web_cache.retrieve(GLD_SCHEMA_URL, filename="schema.xsd")

    assert error.value.code == 503
    assert len(web_cache.calls) == 4
    assert sleeps == [1.0, 2.0, 4.0]


@pytest.mark.parametrize(
    ("url", "error_code"),
    [
        (GLD_SCHEMA_URL, 404),
        (
            "https://www.sec.gov/Archives/edgar/data/1222333/"
            "000143774924036161/gld20240930_10k.htm",
            503,
        ),
        ("https://example.com/gld-20240930.xsd", 503),
    ],
)
def test_does_not_retry_permanent_or_out_of_scope_failures(
    url: str,
    error_code: int,
) -> None:
    error = HTTPError(url, error_code, "failed", hdrs=None, fp=None)
    web_cache = FakeWebCache([error])
    sleeps: list[float] = []
    install_retry(web_cache, sleeps)

    with pytest.raises(HTTPError) as raised:
        web_cache.retrieve(url, filename="download")

    assert raised.value is error
    assert len(web_cache.calls) == 1
    assert sleeps == []


def test_arelle_processor_loads_retry_plugin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_options: dict[str, object] = {}
    transform_plugin = tmp_path / "sec-transform"

    class FakeSession:
        def __enter__(self) -> FakeSession:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def run(self, options: object) -> bool:
            assert options is runtime_options
            return False

        def get_models(self) -> list[object]:
            return []

    def capture_runtime_options(**kwargs: object) -> dict[str, object]:
        runtime_options.update(kwargs)
        return runtime_options

    monkeypatch.setattr(arelle_adapter, "RuntimeOptions", capture_runtime_options)
    monkeypatch.setattr(arelle_adapter, "Session", FakeSession)
    monkeypatch.setattr(
        arelle_adapter,
        "ensure_sec_transform_plugin",
        lambda _cache: transform_plugin,
    )
    company = Company(ticker="GLD", cik="0001222333", name="SPDR GOLD TRUST")
    filing = Filing(
        accession="0001437749-24-036161",
        filing_date=date(2024, 11, 26),
        report_date=date(2024, 9, 30),
        form="10-K",
        primary_document="gld20240930_10k.htm",
        url=(
            "https://www.sec.gov/Archives/edgar/data/1222333/000143774924036161/gld20240930_10k.htm"
        ),
    )

    with pytest.raises(ProcessingError, match="could not load"):
        ArelleProcessor(user_agent="test@example.com").extract_evidence(
            company,
            filing,
            tmp_path / "capture",
        )

    configured_plugins = str(runtime_options["plugins"]).split("|")
    assert configured_plugins == [
        str(transform_plugin),
        str(arelle_adapter._SEC_TAXONOMY_RETRY_PLUGIN),
    ]
    assert arelle_adapter._SEC_TAXONOMY_RETRY_PLUGIN.is_file()
