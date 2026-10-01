CREATE TABLE company_ciks (
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    cik TEXT NOT NULL UNIQUE CHECK (length(cik) = 10),
    legal_name TEXT NOT NULL,
    associated_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (company_id, cik)
);

CREATE TABLE company_cik_transitions (
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    predecessor_cik TEXT NOT NULL,
    successor_cik TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK (predecessor_cik <> successor_cik),
    PRIMARY KEY (company_id, predecessor_cik, successor_cik),
    UNIQUE (company_id, predecessor_cik),
    UNIQUE (company_id, successor_cik),
    FOREIGN KEY (company_id, predecessor_cik)
        REFERENCES company_ciks(company_id, cik) ON DELETE CASCADE,
    FOREIGN KEY (company_id, successor_cik)
        REFERENCES company_ciks(company_id, cik) ON DELETE CASCADE
);

CREATE TABLE filing_provenance (
    filing_id INTEGER PRIMARY KEY REFERENCES filings(id) ON DELETE CASCADE,
    registrant_cik TEXT NOT NULL REFERENCES company_ciks(cik) ON DELETE RESTRICT,
    archive_owner_cik TEXT NOT NULL REFERENCES company_ciks(cik) ON DELETE RESTRICT,
    recorded_at TEXT NOT NULL
);

CREATE INDEX company_cik_transitions_company_idx
    ON company_cik_transitions(company_id, successor_cik, predecessor_cik);
CREATE INDEX filing_provenance_registrant_idx
    ON filing_provenance(registrant_cik, filing_id);
CREATE INDEX filing_provenance_archive_owner_idx
    ON filing_provenance(archive_owner_cik, filing_id);

INSERT INTO company_ciks(company_id, cik, legal_name, associated_at, updated_at)
SELECT id, cik, current_name, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP FROM companies;

INSERT INTO filing_provenance(filing_id, registrant_cik, archive_owner_cik, recorded_at)
SELECT f.id, c.cik, c.cik, CURRENT_TIMESTAMP
FROM filings AS f
JOIN companies AS c ON c.id = f.company_id;
