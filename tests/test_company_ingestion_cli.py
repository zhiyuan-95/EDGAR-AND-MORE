from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

import sec_inline_financials.company_ingestion as company_ingestion
from sec_inline_financials.company_ingestion import CompanyIngestionResult, IngestionSettings
from sec_inline_financials.errors import IngestionError
from sec_inline_financials.models import Company


def _patch_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> IngestionSettings:
    settings = IngestionSettings(
        database_path=tmp_path / "evidence.sqlite3",
        artifact_root=tmp_path / "artifacts",
    )
    monkeypatch.setattr(
        company_ingestion.IngestionSettings,
        "from_environment",
        classmethod(lambda _cls: settings),
    )
    return settings


def _result_for(ticker: str) -> CompanyIngestionResult:
    return CompanyIngestionResult(
        company=Company(ticker=ticker, cik="0000000001", name=f"{ticker} Inc."),
        status="checked_no_update",
        checked_sec=True,
        annual_check_due=True,
        quarterly_check_due=True,
    )


def test_cli_ingests_multiple_space_or_comma_separated_tickers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_environment(monkeypatch, tmp_path)
    calls: list[tuple[str, int, int, bool]] = []

    def fake_ingest_company(
        ticker: str,
        settings: IngestionSettings,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> CompanyIngestionResult:
        assert progress is not None
        calls.append(
            (
                ticker,
                settings.annual_count,
                settings.quarterly_count,
                settings.force_refresh,
            )
        )
        return _result_for(ticker)

    monkeypatch.setattr(company_ingestion, "ingest_company", fake_ingest_company)

    exit_code = company_ingestion.main(
        ["AAPL", "msft,nvda", "--annual-count", "2", "--quarterly-count", "3", "--force"]
    )

    assert exit_code == 0
    assert calls == [
        ("AAPL", 2, 3, True),
        ("MSFT", 2, 3, True),
        ("NVDA", 2, 3, True),
    ]
    output = capsys.readouterr().out
    assert "AAPL: checked_no_update" in output
    assert "MSFT: checked_no_update" in output
    assert "NVDA: checked_no_update" in output


def test_cli_continues_batch_after_one_company_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_environment(monkeypatch, tmp_path)
    calls: list[str] = []

    def fake_ingest_company(
        ticker: str,
        _settings: IngestionSettings,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> CompanyIngestionResult:
        assert progress is not None
        calls.append(ticker)
        if ticker == "FAIL":
            raise IngestionError("simulated failure")
        return _result_for(ticker)

    monkeypatch.setattr(company_ingestion, "ingest_company", fake_ingest_company)

    exit_code = company_ingestion.main(["AAPL", "FAIL", "MSFT"])

    assert exit_code == 1
    assert calls == ["AAPL", "FAIL", "MSFT"]
    output = capsys.readouterr().out
    assert "AAPL: checked_no_update" in output
    assert "FAIL: Error: simulated failure" in output
    assert "MSFT: checked_no_update" in output


def test_cli_rejects_empty_comma_only_batch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_environment(monkeypatch, tmp_path)

    exit_code = company_ingestion.main([","])

    assert exit_code == 1
    assert "Error: Provide at least one company ticker." in capsys.readouterr().out
