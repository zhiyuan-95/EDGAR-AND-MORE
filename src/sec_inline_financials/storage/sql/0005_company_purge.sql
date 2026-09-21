CREATE TABLE pending_file_deletions (
    relative_path TEXT PRIMARY KEY,
    path_kind TEXT NOT NULL CHECK (path_kind IN ('artifact', 'staging_directory')),
    queued_at TEXT NOT NULL
);
