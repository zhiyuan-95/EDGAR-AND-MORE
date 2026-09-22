from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sec_inline_financials.errors import ArtifactError
from sec_inline_financials.evidence_models import FilingSectionRecord
from sec_inline_financials.filing_sections import extract_filing_sections
from sec_inline_financials.storage.artifacts import ArtifactStore
from sec_inline_financials.storage.config import evidence_runtime_paths


@dataclass(frozen=True)
class CompanyChoice:
    company_id: int
    ticker: str
    name: str
    filing_count: int


@dataclass(frozen=True)
class FilingChoice:
    snapshot_id: int
    form: str
    accession: str
    filing_date: str
    report_date: str
    fiscal_year: int | None
    fiscal_period: str | None
    source_url: str
    section_count: int

    @property
    def period_label(self) -> str:
        if self.form == "10-K":
            year = str(self.fiscal_year) if self.fiscal_year is not None else self.report_date[:4]
            return f"FY{year} (report date {self.report_date})"

        if self.fiscal_year is not None and self.fiscal_period:
            return f"FY{self.fiscal_year} {self.fiscal_period} (report date {self.report_date})"
        return f"Period ending {self.report_date}"


@dataclass(frozen=True)
class SectionChoice:
    section_key: str
    section_order: int
    title: str
    filing_count: int


def _connect_read_only(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{database_path.resolve().as_posix()}?mode=ro",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _load_companies(connection: sqlite3.Connection) -> tuple[CompanyChoice, ...]:
    rows = connection.execute(
        """
        SELECT
            c.id AS company_id,
            c.ticker,
            c.current_name,
            count(DISTINCT f.id) AS filing_count
        FROM companies AS c
        JOIN filings AS f ON f.company_id = c.id
        WHERE c.ticker IS NOT NULL
          AND EXISTS (
              SELECT 1
              FROM evidence_snapshots AS s
              WHERE s.filing_id = f.id
          )
        GROUP BY c.id, c.ticker, c.current_name
        ORDER BY upper(c.ticker)
        """
    ).fetchall()
    return tuple(
        CompanyChoice(
            company_id=int(row["company_id"]),
            ticker=str(row["ticker"]).upper(),
            name=str(row["current_name"]),
            filing_count=int(row["filing_count"]),
        )
        for row in rows
    )


def _load_filings(
    connection: sqlite3.Connection,
    company_id: int,
    form: str,
) -> tuple[FilingChoice, ...]:
    rows = connection.execute(
        """
        WITH snapshot_candidates AS (
            SELECT
                f.id AS filing_id,
                f.form,
                f.accession,
                f.filing_date,
                f.report_date,
                f.source_url,
                s.id AS snapshot_id,
                s.fiscal_year,
                s.fiscal_period,
                count(fs.id) AS section_count
            FROM filings AS f
            JOIN evidence_snapshots AS s ON s.filing_id = f.id
            LEFT JOIN filing_sections AS fs ON fs.snapshot_id = s.id
            WHERE f.company_id = ?
              AND f.form = ?
            GROUP BY f.id, s.id
        ),
        ranked_snapshots AS (
            SELECT
                snapshot_candidates.*,
                row_number() OVER (
                    PARTITION BY filing_id
                    ORDER BY
                        CASE WHEN section_count > 0 THEN 0 ELSE 1 END,
                        snapshot_id DESC
                ) AS snapshot_rank
            FROM snapshot_candidates
        )
        SELECT *
        FROM ranked_snapshots
        WHERE snapshot_rank = 1
        ORDER BY report_date, filing_date, accession
        """,
        (company_id, form),
    ).fetchall()
    return tuple(
        FilingChoice(
            snapshot_id=int(row["snapshot_id"]),
            form=str(row["form"]),
            accession=str(row["accession"]),
            filing_date=str(row["filing_date"]),
            report_date=str(row["report_date"]),
            fiscal_year=(int(row["fiscal_year"]) if row["fiscal_year"] is not None else None),
            fiscal_period=(str(row["fiscal_period"]) if row["fiscal_period"] is not None else None),
            source_url=str(row["source_url"]),
            section_count=int(row["section_count"]),
        )
        for row in rows
    )


def _load_section_choices(
    connection: sqlite3.Connection,
    filings: tuple[FilingChoice, ...],
) -> tuple[SectionChoice, ...]:
    placeholders = ", ".join("?" for _ in filings)
    rows = connection.execute(
        f"""
        SELECT
            fs.section_key,
            min(fs.section_order) AS section_order,
            min(fs.title) AS title,
            count(DISTINCT fs.snapshot_id) AS filing_count
        FROM filing_sections AS fs
        WHERE fs.snapshot_id IN ({placeholders})
        GROUP BY fs.section_key
        ORDER BY section_order, fs.section_key
        """,
        tuple(filing.snapshot_id for filing in filings),
    ).fetchall()
    return tuple(
        SectionChoice(
            section_key=str(row["section_key"]),
            section_order=int(row["section_order"]),
            title=str(row["title"]),
            filing_count=int(row["filing_count"]),
        )
        for row in rows
    )


def _load_sections(
    connection: sqlite3.Connection,
    filings: tuple[FilingChoice, ...],
    sections: tuple[SectionChoice, ...],
) -> dict[tuple[int, str], dict[str, object]]:
    snapshot_placeholders = ", ".join("?" for _ in filings)
    section_placeholders = ", ".join("?" for _ in sections)
    parameters = tuple(filing.snapshot_id for filing in filings) + tuple(
        section.section_key for section in sections
    )
    rows = connection.execute(
        f"""
        SELECT
            fs.snapshot_id,
            fs.section_key,
            fs.section_order,
            fs.part,
            fs.item,
            fs.title,
            fs.extraction_status,
            fs.heading_text,
            fs.source_locator_start,
            fs.source_locator_end,
            fs.content_text,
            fs.content_sha256,
            fs.diagnostic,
            sd.document_key,
            sd.original_uri,
            a.sha256 AS artifact_sha256,
            a.relative_object_path,
            a.byte_size AS artifact_byte_size,
            a.media_type AS artifact_media_type
        FROM filing_sections AS fs
        JOIN source_documents AS sd
          ON sd.snapshot_id = fs.snapshot_id
         AND sd.id = fs.source_document_id
        LEFT JOIN artifacts AS a
          ON a.id = sd.artifact_id
        WHERE fs.snapshot_id IN ({snapshot_placeholders})
          AND fs.section_key IN ({section_placeholders})
        ORDER BY fs.snapshot_id, fs.section_order
        """,
        parameters,
    ).fetchall()
    return {(int(row["snapshot_id"]), str(row["section_key"])): dict(row) for row in rows}


def _complete_sections_from_artifacts(
    section_rows: dict[tuple[int, str], dict[str, object]],
    *,
    filings: tuple[FilingChoice, ...],
    artifacts_root: Path,
) -> dict[tuple[int, str], dict[str, object]]:
    filings_by_snapshot = {filing.snapshot_id: filing for filing in filings}
    artifact_store = ArtifactStore(artifacts_root)
    parsed_cache: dict[tuple[str, str], dict[str, FilingSectionRecord]] = {}
    completed: dict[tuple[int, str], dict[str, object]] = {}

    for key, stored_row in section_rows.items():
        snapshot_id, section_key = key
        filing = filings_by_snapshot[snapshot_id]
        document_key = str(stored_row["document_key"])
        relative_path = stored_row["relative_object_path"]
        artifact_sha256 = stored_row["artifact_sha256"]
        artifact_byte_size = stored_row["artifact_byte_size"]
        if relative_path is None or artifact_sha256 is None or artifact_byte_size is None:
            raise RuntimeError(
                f"Snapshot {snapshot_id} section {section_key} has no retained source artifact."
            )

        cache_key = (filing.form, str(relative_path))
        parsed_sections = parsed_cache.get(cache_key)
        if parsed_sections is None:
            try:
                artifact_path = artifact_store.resolve(
                    str(relative_path),
                    str(artifact_sha256),
                    int(artifact_byte_size),
                )
                extracted = extract_filing_sections(
                    artifact_path.read_bytes(),
                    form=filing.form,
                    source_document_key=document_key,
                )
            except ArtifactError as exc:
                raise RuntimeError(
                    f"Could not verify retained source artifact for snapshot {snapshot_id}: {exc}"
                ) from exc
            parsed_sections = {section.section_key: section for section in extracted}
            parsed_cache[cache_key] = parsed_sections

        extracted_section = parsed_sections.get(section_key)
        if extracted_section is None:
            raise RuntimeError(
                f"The retained source artifact for snapshot {snapshot_id} does not define "
                f"section {section_key}."
            )

        row = dict(stored_row)
        row.update(
            {
                "section_order": extracted_section.section_order,
                "part": extracted_section.part,
                "item": extracted_section.item,
                "title": extracted_section.title,
                "extraction_status": extracted_section.extraction_status,
                "heading_text": extracted_section.heading_text,
                "source_locator_start": extracted_section.source_locator_start,
                "source_locator_end": extracted_section.source_locator_end,
                "content_text": extracted_section.content_text,
                "content_sha256": extracted_section.content_sha256,
                "diagnostic": extracted_section.diagnostic,
                "content_basis": "verified retained source artifact (read-only extraction)",
            }
        )
        completed[key] = row
    return completed


def _prompt_company(companies: tuple[CompanyChoice, ...]) -> CompanyChoice:
    print("\nCompanies with stored filing snapshots:")
    for index, company in enumerate(companies, start=1):
        print(
            f"  {index:>2}. {company.ticker:<6} {company.name} ({company.filing_count} filing(s))"
        )

    by_ticker = {company.ticker: company for company in companies}
    while True:
        answer = input("\nCompany (number or ticker): ").strip().upper()
        if answer in by_ticker:
            return by_ticker[answer]
        if answer.isdigit() and 1 <= int(answer) <= len(companies):
            return companies[int(answer) - 1]
        print("Please enter one of the displayed numbers or ticker symbols.")


def _prompt_report_type() -> tuple[str, str]:
    while True:
        answer = input("Annual or quarterly filings? [A/Q]: ").strip().lower()
        if answer in {"a", "annual"}:
            return "annual", "10-K"
        if answer in {"q", "quarter", "quarterly"}:
            return "quarterly", "10-Q"
        print("Please enter A for annual or Q for quarterly.")


def _parse_number_selection(answer: str, item_count: int) -> tuple[int, ...]:
    normalized = answer.strip().lower()
    if normalized == "all":
        return tuple(range(item_count))
    if not normalized:
        raise ValueError("Enter at least one number or 'all'.")

    selected: set[int] = set()
    for token in normalized.split(","):
        token = token.strip()
        if not token:
            raise ValueError("Selections cannot contain an empty item.")
        if "-" in token:
            start_text, end_text = (part.strip() for part in token.split("-", 1))
            if not start_text.isdigit() or not end_text.isdigit():
                raise ValueError(f"Invalid range: {token!r}.")
            start = int(start_text)
            end = int(end_text)
            if start > end:
                raise ValueError(f"Range must run from low to high: {token!r}.")
            numbers = range(start, end + 1)
        elif token.isdigit():
            numbers = (int(token),)
        else:
            raise ValueError(f"Invalid selection: {token!r}.")

        for number in numbers:
            if not 1 <= number <= item_count:
                raise ValueError(f"Selection {number} is outside 1-{item_count}.")
            selected.add(number - 1)

    return tuple(sorted(selected))


def _prompt_number_selection(prompt: str, item_count: int) -> tuple[int, ...]:
    while True:
        try:
            return _parse_number_selection(input(prompt), item_count)
        except ValueError as exc:
            print(f"Invalid selection: {exc}")


def _prompt_filings(filings: tuple[FilingChoice, ...]) -> tuple[FilingChoice, ...]:
    print("\nAvailable stored periods:")
    for index, filing in enumerate(filings, start=1):
        section_note = f"{filing.section_count} stored section(s)"
        print(f"  {index:>2}. {filing.period_label}; filed {filing.filing_date}; {section_note}")

    selected = _prompt_number_selection(
        "\nPeriods (numbers, comma-separated numbers, a range, or 'all'): ",
        len(filings),
    )
    return tuple(filings[index] for index in selected)


def _prompt_sections(
    sections: tuple[SectionChoice, ...],
    filing_count: int,
) -> tuple[SectionChoice, ...]:
    print("\nAvailable sections in the selected filing snapshots:")
    for index, section in enumerate(sections, start=1):
        print(
            f"  {index:>2}. {section.section_key:<18} {section.title} "
            f"({section.filing_count}/{filing_count} filing(s))"
        )

    selected = _prompt_number_selection(
        "\nSections (numbers, comma-separated numbers, a range, or 'all'): ",
        len(sections),
    )
    return tuple(sections[index] for index in selected)


def _format_optional(value: object) -> str:
    return str(value) if value is not None and str(value) else "not available"


def _render_report(
    *,
    database_path: Path,
    company: CompanyChoice,
    report_kind: str,
    filings: tuple[FilingChoice, ...],
    sections: tuple[SectionChoice, ...],
    section_rows: dict[tuple[int, str], Mapping[str, object]],
    generated_at: datetime,
) -> str:
    lines = [
        "STORED SEC FILING SECTION REPORT",
        "=" * 100,
        f"Company: {company.name} ({company.ticker})",
        f"Report type: {report_kind}",
        f"Generated at: {generated_at.isoformat()}",
        f"Evidence database: {database_path.resolve()}",
        f"Selected filings: {len(filings)}",
        "Selected periods: " + ", ".join(filing.period_label for filing in filings),
        "Selected sections: " + ", ".join(section.section_key for section in sections),
        "",
    ]

    extracted_count = 0
    unavailable_count = 0
    for filing_index, filing in enumerate(filings, start=1):
        lines.extend(
            [
                "#" * 100,
                f"FILING {filing_index} OF {len(filings)}",
                "#" * 100,
                f"Period: {filing.period_label}",
                f"Form: {filing.form}",
                f"Accession: {filing.accession}",
                f"Filing date: {filing.filing_date}",
                f"Report date: {filing.report_date}",
                f"Snapshot ID: {filing.snapshot_id}",
                f"SEC source: {filing.source_url}",
                "",
            ]
        )

        for section in sections:
            row = section_rows.get((filing.snapshot_id, section.section_key))
            lines.extend(
                [
                    "-" * 100,
                    f"SECTION: {section.section_key} — {section.title}",
                    "-" * 100,
                ]
            )
            if row is None:
                unavailable_count += 1
                lines.extend(
                    [
                        "Status: not stored for this snapshot",
                        "Content: not available",
                        "",
                    ]
                )
                continue

            status = str(row["extraction_status"])
            if status == "extracted":
                extracted_count += 1
            else:
                unavailable_count += 1
            lines.extend(
                [
                    f"Status: {status}",
                    f"Heading: {_format_optional(row['heading_text'])}",
                    f"Source document: {_format_optional(row['document_key'])}",
                    f"Source URI: {_format_optional(row['original_uri'])}",
                    f"Source start: {_format_optional(row['source_locator_start'])}",
                    f"Source end: {_format_optional(row['source_locator_end'])}",
                    f"Content SHA-256: {_format_optional(row['content_sha256'])}",
                    f"Content basis: {_format_optional(row.get('content_basis'))}",
                    f"Diagnostic: {_format_optional(row['diagnostic'])}",
                    "",
                    "CONTENT",
                    "",
                    str(row["content_text"] or "[No extracted section text is available.]"),
                    "",
                ]
            )

    lines.extend(
        [
            "=" * 100,
            "REPORT SUMMARY",
            "=" * 100,
            f"Filings included: {len(filings)}",
            f"Section types selected: {len(sections)}",
            f"Extracted filing-section records: {extracted_count}",
            f"Unavailable filing-section records: {unavailable_count}",
            "",
        ]
    )
    return "\n".join(lines)


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _write_report(
    report_text: str,
    *,
    company: CompanyChoice,
    report_kind: str,
    all_filings_selected: bool,
    all_sections_selected: bool,
    generated_at: datetime,
) -> Path:
    output_directory = Path("output")
    output_directory.mkdir(parents=True, exist_ok=True)
    period_slug = "all_periods" if all_filings_selected else "selected_periods"
    section_slug = "all_sections" if all_sections_selected else "selected_sections"
    timestamp = generated_at.strftime("%Y%m%dT%H%M%SZ")
    filename = (
        f"{_slug(company.ticker)}_{_slug(report_kind)}_{period_slug}_{section_slug}_{timestamp}.txt"
    )
    report_path = output_directory / filename
    report_path.write_text(report_text, encoding="utf-8")
    return report_path.resolve()


def main() -> int:
    try:
        runtime_paths = evidence_runtime_paths(os.environ)
        if not runtime_paths.database.is_file():
            raise RuntimeError(
                f"Evidence database does not exist: {runtime_paths.database.resolve()}"
            )

        print(f"Reading stored evidence from: {runtime_paths.database.resolve()}")
        print(
            "This inspection does not contact SEC or run Arelle; selected sections are "
            "rebuilt read-only from verified retained filing artifacts."
        )

        with closing(_connect_read_only(runtime_paths.database)) as connection:
            companies = _load_companies(connection)
            if not companies:
                raise RuntimeError("No companies with stored filing snapshots were found.")
            company = _prompt_company(companies)
            report_kind, form = _prompt_report_type()

            available_filings = _load_filings(connection, company.company_id, form)
            if not available_filings:
                raise RuntimeError(f"No stored {form} filing snapshots found for {company.ticker}.")
            selected_filings = _prompt_filings(available_filings)

            available_sections = _load_section_choices(connection, selected_filings)
            if not available_sections:
                raise RuntimeError(
                    f"The selected {company.ticker} {form} snapshots contain no narrative "
                    "filing sections. They may use the legacy evidence-v1 extraction profile."
                )
            selected_sections = _prompt_sections(available_sections, len(selected_filings))
            section_rows = _load_sections(connection, selected_filings, selected_sections)
            section_rows = _complete_sections_from_artifacts(
                section_rows,
                filings=selected_filings,
                artifacts_root=runtime_paths.artifacts,
            )

        generated_at = datetime.now(timezone.utc)
        report_text = _render_report(
            database_path=runtime_paths.database,
            company=company,
            report_kind=report_kind,
            filings=selected_filings,
            sections=selected_sections,
            section_rows=section_rows,
            generated_at=generated_at,
        )
        report_path = _write_report(
            report_text,
            company=company,
            report_kind=report_kind,
            all_filings_selected=len(selected_filings) == len(available_filings),
            all_sections_selected=len(selected_sections) == len(available_sections),
            generated_at=generated_at,
        )

        print("\nReport created successfully.")
        print(f"Filings included: {len(selected_filings)}")
        print(f"Section types included: {len(selected_sections)}")
        print(f"TXT report: {report_path}")
        return 0
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return 130
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        print(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
