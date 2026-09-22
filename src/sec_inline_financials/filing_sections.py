from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Literal

from lxml import etree

from sec_inline_financials.evidence_models import FilingSectionRecord

_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "blockquote",
        "dd",
        "div",
        "dl",
        "dt",
        "figcaption",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "li",
        "main",
        "nav",
        "p",
        "pre",
        "section",
        "td",
        "th",
    }
)
_ITEM_HEADING = re.compile(
    r"^item\s+(?P<item>\d{1,2}[a-z]?)\b[\s.:–—-]*(?P<title>.*)$",
    re.IGNORECASE,
)
_PART_HEADING = re.compile(
    r"^part\s+(?P<part>i{1,3}|iv|v)\b(?:[\s.:–—-]+.*)?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _SectionDefinition:
    section_key: str
    order: int
    item: str
    title: str
    part: str | None = None


@dataclass(frozen=True)
class _TextBlock:
    text: str
    locator: str


@dataclass(frozen=True)
class _Heading:
    block_index: int
    kind: Literal["part", "item"]
    text: str
    locator: str
    item: str | None = None
    part: str | None = None


_DEFINITIONS: dict[str, tuple[_SectionDefinition, ...]] = {
    "10-K": (
        _SectionDefinition("item_1", 0, "1", "Business"),
        _SectionDefinition("item_1a", 1, "1A", "Risk Factors"),
        _SectionDefinition("item_3", 2, "3", "Legal Proceedings"),
        _SectionDefinition("item_7", 3, "7", "Management's Discussion and Analysis"),
        _SectionDefinition("item_7a", 4, "7A", "Market Risk"),
        _SectionDefinition("item_8", 5, "8", "Financial Statements and Notes"),
    ),
    "10-Q": (
        _SectionDefinition("part_i_item_1", 0, "1", "Financial Statements", "I"),
        _SectionDefinition("part_i_item_2", 1, "2", "Management's Discussion and Analysis", "I"),
        _SectionDefinition("part_i_item_3", 2, "3", "Market Risk", "I"),
        _SectionDefinition("part_i_item_4", 3, "4", "Controls and Procedures", "I"),
        _SectionDefinition("part_ii_item_1", 4, "1", "Legal Proceedings", "II"),
        _SectionDefinition("part_ii_item_1a", 5, "1A", "Risk Factors", "II"),
    ),
}


def extract_filing_sections(
    source: bytes,
    *,
    form: str,
    source_document_key: str,
) -> tuple[FilingSectionRecord, ...]:
    """Extract required 10-K/10-Q item text from one retained primary document."""
    definitions = _DEFINITIONS.get(form)
    if definitions is None:
        return ()
    try:
        parser = etree.HTMLParser(recover=True, no_network=True, huge_tree=True)
        root = etree.fromstring(source, parser=parser)
        if root is None:
            raise ValueError("HTML parser returned no document root")
        blocks = _text_blocks(root)
        headings = _headings(blocks)
    except (etree.LxmlError, TypeError, ValueError) as exc:
        return tuple(
            _missing_record(
                definition,
                source_document_key,
                status="parse_error",
                diagnostic=f"{type(exc).__name__}: {exc}",
            )
            for definition in definitions
        )

    records: list[FilingSectionRecord] = []
    for definition in definitions:
        candidates = [
            (index, heading)
            for index, heading in enumerate(headings)
            if heading.kind == "item"
            and heading.item == definition.item
            and (definition.part is None or heading.part == definition.part)
        ]
        if not candidates and definition.part is not None:
            candidates = [
                (index, heading)
                for index, heading in enumerate(headings)
                if heading.kind == "item"
                and heading.item == definition.item
                and heading.part is None
            ]
        extracted = [
            _candidate_record(
                definition,
                heading,
                heading_index=index,
                headings=headings,
                blocks=blocks,
                source_document_key=source_document_key,
            )
            for index, heading in candidates
        ]
        usable = [record for record in extracted if record.content_text]
        if usable:
            records.append(max(usable, key=lambda record: len(record.content_text or "")))
        else:
            records.append(_missing_record(definition, source_document_key))
    return tuple(records)


def _text_blocks(root: etree._Element) -> tuple[_TextBlock, ...]:
    tree = root.getroottree()
    blocks: list[_TextBlock] = []
    for element in root.iter():
        if _local_name(element.tag) not in _BLOCK_TAGS:
            continue
        if any(
            _local_name(descendant.tag) in _BLOCK_TAGS for descendant in element.iterdescendants()
        ):
            continue
        text = _visible_text(element)
        if text:
            blocks.append(_TextBlock(text=text, locator=str(tree.getpath(element))))
    return tuple(blocks)


def _visible_text(element: etree._Element) -> str:
    chunks = element.xpath(
        ".//text()[not(ancestor::*[local-name()='script' or "
        "local-name()='style' or local-name()='noscript'])]"
    )
    return " ".join(part for value in chunks if (part := " ".join(str(value).split())))


def _headings(blocks: tuple[_TextBlock, ...]) -> tuple[_Heading, ...]:
    headings: list[_Heading] = []
    current_part: str | None = None
    for index, block in enumerate(blocks):
        normalized = " ".join(block.text.replace("\xa0", " ").split())
        if len(normalized) <= 240 and (part_match := _PART_HEADING.fullmatch(normalized)):
            current_part = part_match.group("part").upper()
            headings.append(
                _Heading(
                    block_index=index,
                    kind="part",
                    text=block.text,
                    locator=block.locator,
                    part=current_part,
                )
            )
            continue
        if len(normalized) > 320 or (item_match := _ITEM_HEADING.match(normalized)) is None:
            continue
        headings.append(
            _Heading(
                block_index=index,
                kind="item",
                text=block.text,
                locator=block.locator,
                item=item_match.group("item").upper(),
                part=current_part,
            )
        )
    return tuple(headings)


def _candidate_record(
    definition: _SectionDefinition,
    heading: _Heading,
    *,
    heading_index: int,
    headings: tuple[_Heading, ...],
    blocks: tuple[_TextBlock, ...],
    source_document_key: str,
) -> FilingSectionRecord:
    next_heading = next(
        (
            candidate
            for candidate in headings[heading_index + 1 :]
            if _is_section_boundary(definition, heading, candidate)
        ),
        None,
    )
    end_index = next_heading.block_index if next_heading is not None else len(blocks)
    content_text = "\n\n".join(
        block.text for block in blocks[heading.block_index : end_index] if block.text
    ).strip()
    return FilingSectionRecord(
        section_key=definition.section_key,
        section_order=definition.order,
        part=definition.part,
        item=definition.item,
        title=definition.title,
        extraction_status="extracted",
        source_document_key=source_document_key,
        heading_text=heading.text,
        source_locator_start=heading.locator,
        source_locator_end=next_heading.locator if next_heading is not None else None,
        content_text=content_text,
        content_sha256=hashlib.sha256(content_text.encode("utf-8")).hexdigest(),
    )


def _is_section_boundary(
    definition: _SectionDefinition,
    heading: _Heading,
    candidate: _Heading,
) -> bool:
    if candidate.kind == "item":
        return candidate.item != heading.item
    if definition.part is None:
        return False
    current_part = heading.part or definition.part
    return candidate.part != current_part


def _missing_record(
    definition: _SectionDefinition,
    source_document_key: str,
    *,
    status: Literal["not_found", "parse_error"] = "not_found",
    diagnostic: str | None = None,
) -> FilingSectionRecord:
    return FilingSectionRecord(
        section_key=definition.section_key,
        section_order=definition.order,
        part=definition.part,
        item=definition.item,
        title=definition.title,
        extraction_status=status,
        source_document_key=source_document_key,
        diagnostic=diagnostic,
    )


def _local_name(tag: object) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", maxsplit=1)[-1].casefold()
