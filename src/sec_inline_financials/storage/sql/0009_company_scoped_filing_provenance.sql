CREATE UNIQUE INDEX filings_id_company_idx
    ON filings(id, company_id);

CREATE TABLE filing_provenance_new (
    filing_id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL,
    registrant_cik TEXT NOT NULL,
    archive_owner_cik TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    FOREIGN KEY (filing_id, company_id)
        REFERENCES filings(id, company_id) ON DELETE CASCADE,
    FOREIGN KEY (company_id, registrant_cik)
        REFERENCES company_ciks(company_id, cik) ON DELETE RESTRICT,
    FOREIGN KEY (company_id, archive_owner_cik)
        REFERENCES company_ciks(company_id, cik) ON DELETE RESTRICT
);

CREATE TRIGGER filing_provenance_company_insert_check
BEFORE INSERT ON filing_provenance_new
WHEN NOT EXISTS (
    SELECT 1 FROM filings AS f
    WHERE f.id = NEW.filing_id AND f.company_id = NEW.company_id
)
OR NOT EXISTS (
    SELECT 1 FROM company_ciks AS cc
    WHERE cc.company_id = NEW.company_id AND cc.cik = NEW.registrant_cik
)
OR NOT EXISTS (
    SELECT 1 FROM company_ciks AS cc
    WHERE cc.company_id = NEW.company_id AND cc.cik = NEW.archive_owner_cik
)
BEGIN
    SELECT RAISE(ABORT, 'filing provenance CIKs must belong to the filing company');
END;

CREATE TRIGGER filing_provenance_company_update_check
BEFORE UPDATE ON filing_provenance_new
WHEN NOT EXISTS (
    SELECT 1 FROM filings AS f
    WHERE f.id = NEW.filing_id AND f.company_id = NEW.company_id
)
OR NOT EXISTS (
    SELECT 1 FROM company_ciks AS cc
    WHERE cc.company_id = NEW.company_id AND cc.cik = NEW.registrant_cik
)
OR NOT EXISTS (
    SELECT 1 FROM company_ciks AS cc
    WHERE cc.company_id = NEW.company_id AND cc.cik = NEW.archive_owner_cik
)
BEGIN
    SELECT RAISE(ABORT, 'filing provenance CIKs must belong to the filing company');
END;

INSERT INTO filing_provenance_new(
    filing_id, company_id, registrant_cik, archive_owner_cik, recorded_at
)
SELECT
    fp.filing_id,
    f.company_id,
    fp.registrant_cik,
    fp.archive_owner_cik,
    fp.recorded_at
FROM filing_provenance AS fp
LEFT JOIN filings AS f ON f.id = fp.filing_id;

DROP TABLE filing_provenance;
ALTER TABLE filing_provenance_new RENAME TO filing_provenance;

CREATE INDEX filing_provenance_registrant_idx
    ON filing_provenance(company_id, registrant_cik, filing_id);
CREATE INDEX filing_provenance_archive_owner_idx
    ON filing_provenance(company_id, archive_owner_cik, filing_id);

CREATE INDEX company_cik_transitions_successor_effective_idx
    ON company_cik_transitions(successor_cik, effective_date);
