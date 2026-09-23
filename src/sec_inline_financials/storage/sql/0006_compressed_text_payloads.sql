CREATE TABLE text_payloads (
    sha256 TEXT PRIMARY KEY CHECK (length(sha256) = 64),
    text_encoding TEXT NOT NULL CHECK (text_encoding = 'utf-8'),
    compression_codec TEXT NOT NULL CHECK (compression_codec = 'zlib'),
    original_byte_size INTEGER NOT NULL CHECK (original_byte_size >= 0),
    compressed_byte_size INTEGER NOT NULL CHECK (compressed_byte_size >= 0),
    compressed_bytes BLOB NOT NULL,
    created_at TEXT NOT NULL,
    CHECK (compressed_byte_size = length(compressed_bytes))
);

ALTER TABLE facts ADD COLUMN raw_value_payload_sha256 TEXT
    REFERENCES text_payloads(sha256)
    CHECK (raw_value_payload_sha256 IS NULL OR length(raw_value_payload_sha256) = 64);

CREATE INDEX facts_raw_value_payload_idx ON facts(raw_value_payload_sha256)
    WHERE raw_value_payload_sha256 IS NOT NULL;

CREATE TRIGGER facts_raw_value_payload_insert_check
BEFORE INSERT ON facts
WHEN NEW.raw_value_text IS NOT NULL AND NEW.raw_value_payload_sha256 IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'fact raw value must be inline or payload-backed, not both');
END;

CREATE TRIGGER facts_raw_value_payload_update_check
BEFORE UPDATE OF raw_value_text, raw_value_payload_sha256 ON facts
WHEN NEW.raw_value_text IS NOT NULL AND NEW.raw_value_payload_sha256 IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'fact raw value must be inline or payload-backed, not both');
END;
