ALTER TABLE companies ADD COLUMN latest_10k_filing_date TEXT;
ALTER TABLE companies ADD COLUMN latest_10q_filing_date TEXT;
ALTER TABLE companies ADD COLUMN next_check_date_10k TEXT;
ALTER TABLE companies ADD COLUMN next_check_date_10q TEXT;

ALTER TABLE filings ADD COLUMN is_active INTEGER NOT NULL DEFAULT 0
    CHECK (is_active IN (0, 1));
ALTER TABLE filings ADD COLUMN active_window_rank INTEGER
    CHECK (active_window_rank IS NULL OR active_window_rank > 0);

CREATE UNIQUE INDEX companies_ticker_unique_idx
    ON companies(upper(ticker))
    WHERE ticker IS NOT NULL;
CREATE INDEX filings_active_window_idx
    ON filings(company_id, form, is_active, active_window_rank);
