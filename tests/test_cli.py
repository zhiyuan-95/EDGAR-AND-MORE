from pathlib import Path

from sec_inline_financials.cli import run_interactive
from sec_inline_financials.errors import DiscoveryError


def test_cli_reports_the_separate_annual_and_twelve_quarter_paths(tmp_path: Path) -> None:
    answers = iter([" aapl ", "5"])
    output: list[str] = []
    received: list[tuple[str, int, Path]] = []

    def generate_reports(ticker: str, years: int, output_dir: Path) -> tuple[Path, Path]:
        received.append((ticker, years, output_dir))
        annual_path = output_dir / "AAPL_latest_5_years.txt"
        quarterly_path = output_dir / "AAPL_latest_12_10q_quarters.txt"
        return annual_path, quarterly_path

    exit_code = run_interactive(
        input_fn=lambda _prompt: next(answers),
        output_fn=output.append,
        generate_reports=generate_reports,
        output_dir=tmp_path,
    )

    assert exit_code == 0
    assert received == [("AAPL", 5, tmp_path)]
    assert output == [
        f"Annual report saved to: {tmp_path / 'AAPL_latest_5_years.txt'}",
        f"Quarterly report saved to: {tmp_path / 'AAPL_latest_12_10q_quarters.txt'}",
    ]


def test_cli_rejects_invalid_input_before_generation(tmp_path: Path) -> None:
    answers = iter(["AAPL!", "0"])
    output: list[str] = []
    generated = False

    def generate_reports(_ticker: str, _years: int, _output_dir: Path) -> tuple[Path, Path]:
        nonlocal generated
        generated = True
        raise AssertionError("generation must not run")

    exit_code = run_interactive(
        input_fn=lambda _prompt: next(answers),
        output_fn=output.append,
        generate_reports=generate_reports,
        output_dir=tmp_path,
    )

    assert exit_code == 2
    assert generated is False
    assert output == ["Error: ticker must contain 1-10 letters, digits, dots, or hyphens."]


def test_cli_presents_domain_errors_without_a_traceback(tmp_path: Path) -> None:
    answers = iter(["MISSING", "5"])
    output: list[str] = []

    def generate_reports(_ticker: str, _years: int, _output_dir: Path) -> tuple[Path, Path]:
        raise DiscoveryError("Ticker MISSING was not found in the SEC ticker file.")

    exit_code = run_interactive(
        input_fn=lambda _prompt: next(answers),
        output_fn=output.append,
        generate_reports=generate_reports,
        output_dir=tmp_path,
    )

    assert exit_code == 1
    assert output == ["Error: Ticker MISSING was not found in the SEC ticker file."]
