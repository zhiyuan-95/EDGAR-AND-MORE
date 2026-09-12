from __future__ import annotations

import hashlib

from sec_inline_financials.filing_sections import extract_filing_sections


def _by_key(source: bytes, form: str):
    return {
        section.section_key: section
        for section in extract_filing_sections(
            source,
            form=form,
            source_document_key="primary",
        )
    }


def test_extracts_required_10k_items_and_ignores_short_table_of_contents_entries() -> None:
    source = b"""
    <html><body>
      <nav>
        <p>Item 1. Business</p><p>Item 1A. Risk Factors</p><p>Item 3. Legal Proceedings</p>
        <p>Item 7. Management's Discussion and Analysis</p>
        <p>Item 7A. Market Risk</p><p>Item 8. Financial Statements</p>
      </nav>
      <h1>Item 1. Business</h1><p>We manufacture orbital widgets for commercial customers.</p>
      <h1>Item 1A. Risk Factors</h1><p>Supply constraints could interrupt widget production.</p>
      <h1>Item 1B. Unresolved Staff Comments</h1><p>None.</p>
      <h1>Item 2. Properties</h1><p>Factory locations.</p>
      <h1>Item 3. Legal Proceedings</h1><p>A contract claim remains pending.</p>
      <h1>Item 4. Mine Safety Disclosures</h1><p>Not applicable.</p>
      <h1>Item 5. Market for Registrant's Common Equity</h1><p>Market information.</p>
      <h1>Item 6. Reserved</h1><p>Reserved.</p>
      <h1>Item 7. Management's Discussion and Analysis</h1>
      <p>Management explains revenue growth and liquidity.</p>
      <h1>Item 7A. Quantitative and Qualitative Disclosures About Market Risk</h1>
      <p>Interest-rate sensitivity is monitored monthly.</p>
      <h1>Item 8. Financial Statements and Supplementary Data</h1>
      <p>Consolidated statements and accompanying note disclosures.</p>
      <h1>Item 9. Changes in and Disagreements With Accountants</h1><p>None.</p>
    </body></html>
    """

    sections = _by_key(source, "10-K")

    assert tuple(sections) == ("item_1", "item_1a", "item_3", "item_7", "item_7a", "item_8")
    assert all(section.extraction_status == "extracted" for section in sections.values())
    assert "orbital widgets" in (sections["item_1"].content_text or "")
    assert "Supply constraints" not in (sections["item_1"].content_text or "")
    assert "contract claim" in (sections["item_3"].content_text or "")
    assert "revenue growth" in (sections["item_7"].content_text or "")
    assert "Interest-rate sensitivity" in (sections["item_7a"].content_text or "")
    assert "accompanying note disclosures" in (sections["item_8"].content_text or "")
    assert (
        sections["item_8"].content_sha256
        == hashlib.sha256((sections["item_8"].content_text or "").encode("utf-8")).hexdigest()
    )


def test_extracts_10q_items_with_part_aware_duplicate_item_numbers() -> None:
    source = b"""
    <html><body>
      <h1>Part I. Financial Information</h1>
      <h2>Item 1. Financial Statements</h2><p>Quarterly balance sheets and footnotes.</p>
      <h2>Item 2. Management's Discussion and Analysis</h2><p>Quarterly operating trends.</p>
      <h2>Item 3. Quantitative and Qualitative Disclosures About Market Risk</h2>
      <p>Foreign-exchange exposure.</p>
      <h2>Item 4. Controls and Procedures</h2><p>Disclosure controls were effective.</p>
      <h1>Part II. Other Information</h1>
      <h2>Item 1. Legal Proceedings</h2><p>A patent matter is pending.</p>
      <h2>Item 1A. Risk Factors</h2><p>Cybersecurity threats may increase costs.</p>
      <h2>Item 2. Unregistered Sales of Equity Securities</h2><p>None.</p>
    </body></html>
    """

    sections = _by_key(source, "10-Q")

    assert tuple(sections) == (
        "part_i_item_1",
        "part_i_item_2",
        "part_i_item_3",
        "part_i_item_4",
        "part_ii_item_1",
        "part_ii_item_1a",
    )
    assert "balance sheets" in (sections["part_i_item_1"].content_text or "")
    assert "patent matter" not in (sections["part_i_item_1"].content_text or "")
    assert "patent matter" in (sections["part_ii_item_1"].content_text or "")
    assert "Cybersecurity threats" in (sections["part_ii_item_1a"].content_text or "")
    assert sections["part_i_item_4"].source_locator_end is not None


def test_missing_and_unparseable_sections_are_explicit() -> None:
    missing = extract_filing_sections(
        b"<html><body><p>Cover page only</p></body></html>",
        form="10-K",
        source_document_key="primary",
    )
    parse_error = extract_filing_sections(
        b"",
        form="10-Q",
        source_document_key="primary",
    )

    assert len(missing) == 6
    assert {section.extraction_status for section in missing} == {"not_found"}
    assert len(parse_error) == 6
    assert {section.extraction_status for section in parse_error} == {"parse_error"}
    assert all(section.diagnostic for section in parse_error)
