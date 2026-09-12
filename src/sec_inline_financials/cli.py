from __future__ import annotations

from collections.abc import Sequence

from sec_inline_financials.company_ingestion import main as run_company_ingestion


def main(argv: Sequence[str] | None = None) -> int:
    """Run the real SEC company-ingestion command."""
    return run_company_ingestion(argv)


if __name__ == "__main__":
    raise SystemExit(main())
