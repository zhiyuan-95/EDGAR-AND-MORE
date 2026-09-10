class ExplorerError(Exception):
    """Base class for errors that can be presented directly at the CLI."""


class DiscoveryError(ExplorerError):
    """SEC company or filing discovery could not satisfy the request."""


class ProcessingError(ExplorerError):
    """Arelle could not produce a usable annual result."""


class IngestionError(ExplorerError):
    """A company ingestion could not publish any usable filing evidence."""


class EvidenceStorageError(ExplorerError):
    """Base class for durable evidence storage failures."""


class SchemaError(EvidenceStorageError):
    """The evidence database schema is incompatible or could not be migrated."""


class DatabaseBusyError(EvidenceStorageError):
    """SQLite could not obtain the required lock within the configured timeout."""


class CaptureError(EvidenceStorageError):
    """A required filing resource could not be captured exactly."""


class ExtractionError(EvidenceStorageError):
    """Arelle evidence could not be detached without loss."""


class SnapshotMismatchError(EvidenceStorageError):
    """An immutable snapshot identity matched but its payload did not."""


class FilingMetadataError(EvidenceStorageError):
    """A known accession was rediscovered with different immutable metadata."""


class ArtifactError(EvidenceStorageError):
    """A retained artifact is missing, unsafe, or fails integrity checks."""


class MissingArtifactError(ArtifactError):
    """A retained artifact cannot be found."""


class ArtifactHashMismatchError(ArtifactError):
    """A retained artifact's bytes do not match its recorded hash."""
