from __future__ import annotations

import builtins
import hashlib
import importlib.util
import sqlite3
import sys
from pathlib import Path
from types import ModuleType

from sec_inline_financials.filing_sections import extract_filing_sections


def test_extract_filing_sections_joins_repeated_page_headers() -> None:
    source = b"""
    <html><body>
      <p>PART II</p>
      <p>ITEM 7. MANAGEMENT'S DISCUSSION AND ANALYSIS</p>
      <p>Opening discussion from the first page.</p>
      <p>PART II</p>
      <p>Item 7</p>
      <p>Middle discussion from the second page.</p>
      <p>PART II</p>
      <p>Item 7</p>
      <p>Final discussion from the third page.</p>
      <p>ITEM 7A. QUANTITATIVE AND QUALITATIVE DISCLOSURES</p>
      <p>Market-risk content must not be included in Item 7.</p>
      <p>ITEM 8. FINANCIAL STATEMENTS AND SUPPLEMENTARY DATA</p>
    </body></html>
    """

    sections = extract_filing_sections(
        source,
        form="10-K",
        source_document_key="primary-document",
    )
    item_7 = next(section for section in sections if section.section_key == "item_7")

    assert item_7.extraction_status == "extracted"
    assert item_7.content_text is not None
    assert "Opening discussion from the first page." in item_7.content_text
    assert "Middle discussion from the second page." in item_7.content_text
    assert "Final discussion from the third page." in item_7.content_text
    assert "Market-risk content must not be included in Item 7." not in item_7.content_text
    assert item_7.content_text.index("Opening discussion") < item_7.content_text.index(
        "Final discussion"
    )


def test_extract_filing_sections_keeps_quarterly_parts_separate() -> None:
    source = b"""
    <html><body>
      <p>PART I</p>
      <p>ITEM 1. FINANCIAL STATEMENTS</p>
      <p>First page of Part I financial statements.</p>
      <p>PART I</p>
      <p>Item 1</p>
      <p>Second page of Part I financial statements.</p>
      <p>ITEM 2. MANAGEMENT'S DISCUSSION AND ANALYSIS</p>
      <p>Part I management discussion.</p>
      <p>PART II</p>
      <p>ITEM 1. LEGAL PROCEEDINGS</p>
      <p>First page of Part II legal proceedings.</p>
      <p>PART II</p>
      <p>Item 1</p>
      <p>Second page of Part II legal proceedings.</p>
      <p>ITEM 1A. RISK FACTORS</p>
      <p>Part II risk-factor content.</p>
      <p>ITEM 2. UNREGISTERED SALES OF EQUITY SECURITIES</p>
    </body></html>
    """

    sections = extract_filing_sections(
        source,
        form="10-Q",
        source_document_key="primary-document",
    )
    by_key = {section.section_key: section for section in sections}

    part_i_item_1 = by_key["part_i_item_1"].content_text or ""
    assert "First page of Part I financial statements." in part_i_item_1
    assert "Second page of Part I financial statements." in part_i_item_1
    assert "Part I management discussion." not in part_i_item_1
    assert "Part II legal proceedings." not in part_i_item_1

    part_ii_item_1 = by_key["part_ii_item_1"].content_text or ""
    assert "First page of Part II legal proceedings." in part_ii_item_1
    assert "Second page of Part II legal proceedings." in part_ii_item_1
    assert "Part II risk-factor content." not in part_ii_item_1


def test_inspect_filings_uses_complete_text_from_retained_artifact(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    source = b"""
    <html><body>
      <p>PART II</p>
      <p>ITEM 7. MANAGEMENT'S DISCUSSION AND ANALYSIS</p>
      <p>Opening discussion from the first page.</p>
      <p>PART II</p>
      <p>Item 7</p>
      <p>Middle discussion from the second page.</p>
      <p>PART II</p>
      <p>Item 7</p>
      <p>Final discussion from the third page.</p>
      <p>ITEM 7A. QUANTITATIVE AND QUALITATIVE DISCLOSURES</p>
    </body></html>
    """
    digest = hashlib.sha256(source).hexdigest()
    relative_path = Path("objects") / "sha256" / digest[:2] / digest
    artifact_path = tmp_path / relative_path
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(source)

    database_path = tmp_path / "evidence.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE companies (
                id INTEGER PRIMARY KEY,
                ticker TEXT,
                current_name TEXT NOT NULL
            );
            CREATE TABLE filings (
                id INTEGER PRIMARY KEY,
                company_id INTEGER NOT NULL,
                form TEXT NOT NULL,
                accession TEXT NOT NULL,
                filing_date TEXT NOT NULL,
                report_date TEXT NOT NULL,
                source_url TEXT NOT NULL
            );
            CREATE TABLE evidence_snapshots (
                id INTEGER PRIMARY KEY,
                filing_id INTEGER NOT NULL,
                fiscal_year INTEGER,
                fiscal_period TEXT
            );
            CREATE TABLE artifacts (
                id INTEGER PRIMARY KEY,
                sha256 TEXT NOT NULL,
                relative_object_path TEXT NOT NULL,
                byte_size INTEGER NOT NULL,
                media_type TEXT NOT NULL
            );
            CREATE TABLE source_documents (
                id INTEGER PRIMARY KEY,
                snapshot_id INTEGER NOT NULL,
                document_key TEXT NOT NULL,
                original_uri TEXT NOT NULL,
                artifact_id INTEGER
            );
            CREATE TABLE filing_sections (
                id INTEGER PRIMARY KEY,
                snapshot_id INTEGER NOT NULL,
                section_key TEXT NOT NULL,
                section_order INTEGER NOT NULL,
                part TEXT,
                item TEXT NOT NULL,
                title TEXT NOT NULL,
                extraction_status TEXT NOT NULL,
                source_document_id INTEGER NOT NULL,
                heading_text TEXT,
                source_locator_start TEXT,
                source_locator_end TEXT,
                content_text TEXT,
                content_sha256 TEXT,
                diagnostic TEXT
            );
            """
        )
        connection.execute(
            "INSERT INTO companies(id, ticker, current_name) VALUES (1, 'MSFT', 'Microsoft')"
        )
        connection.execute(
            """
            INSERT INTO filings(
                id, company_id, form, accession, filing_date, report_date, source_url
            ) VALUES (1, 1, '10-K', 'example-accession', '2022-07-28', '2022-06-30',
                'https://www.sec.gov/example')
            """
        )
        connection.execute(
            """
            INSERT INTO evidence_snapshots(id, filing_id, fiscal_year, fiscal_period)
            VALUES (1, 1, 2022, 'FY')
            """
        )
        connection.execute(
            """
            INSERT INTO artifacts(
                id, sha256, relative_object_path, byte_size, media_type
            ) VALUES (1, ?, ?, ?, 'text/html')
            """,
            (digest, relative_path.as_posix(), len(source)),
        )
        connection.execute(
            """
            INSERT INTO source_documents(
                id, snapshot_id, document_key, original_uri, artifact_id
            ) VALUES (1, 1, 'primary-document', 'https://www.sec.gov/example', 1)
            """
        )
        stored_fragment = "ITEM 7\n\nOpening discussion from the first page."
        connection.execute(
            """
            INSERT INTO filing_sections(
                snapshot_id, section_key, section_order, part, item, title,
                extraction_status, source_document_id, heading_text,
                source_locator_start, source_locator_end, content_text,
                content_sha256, diagnostic
            ) VALUES (
                1, 'item_7', 3, NULL, '7', 'Management''s Discussion and Analysis',
                'extracted', 1, 'ITEM 7', '/html/body/p[2]', '/html/body/p[4]', ?, ?, NULL
            )
            """,
            (stored_fragment, hashlib.sha256(stored_fragment.encode()).hexdigest()),
        )

    module = _load_inspector_module()
    answers = iter(("MSFT", "A", "all", "all"))
    monkeypatch.setattr(builtins, "input", lambda _prompt="": next(answers))
    monkeypatch.setenv("SEC_INLINE_FINANCIALS_DATA_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    assert module.main() == 0
    report_path = next((tmp_path / "output").glob("*.txt"))
    report = report_path.read_text(encoding="utf-8")
    assert "Opening discussion from the first page." in report
    assert "Middle discussion from the second page." in report
    assert "Final discussion from the third page." in report

    with sqlite3.connect(database_path) as connection:
        stored = connection.execute(
            "SELECT content_text FROM filing_sections WHERE section_key = 'item_7'"
        ).fetchone()
    assert stored == (stored_fragment,)


def _load_inspector_module() -> ModuleType:
    script_path = Path(__file__).with_name("inspect_filings.py")
    spec = importlib.util.spec_from_file_location("inspect_filings_script", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
