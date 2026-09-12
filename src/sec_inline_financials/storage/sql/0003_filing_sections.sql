CREATE TABLE filing_sections (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES evidence_snapshots(id),
    section_key TEXT NOT NULL,
    section_order INTEGER NOT NULL CHECK (section_order >= 0),
    part TEXT,
    item TEXT NOT NULL,
    title TEXT NOT NULL,
    extraction_status TEXT NOT NULL CHECK (
        extraction_status IN ('extracted', 'not_found', 'parse_error')
    ),
    source_document_id INTEGER NOT NULL,
    heading_text TEXT,
    source_locator_start TEXT,
    source_locator_end TEXT,
    content_text TEXT,
    content_sha256 TEXT,
    diagnostic TEXT,
    UNIQUE (snapshot_id, section_key),
    UNIQUE (snapshot_id, section_order),
    FOREIGN KEY (snapshot_id, source_document_id)
        REFERENCES source_documents(snapshot_id, id),
    CHECK (
        (extraction_status = 'extracted' AND content_text IS NOT NULL
            AND content_sha256 IS NOT NULL AND source_locator_start IS NOT NULL)
        OR
        (extraction_status != 'extracted' AND content_text IS NULL
            AND content_sha256 IS NULL)
    )
);

CREATE INDEX filing_sections_item_idx
    ON filing_sections(snapshot_id, part, item, section_order);
