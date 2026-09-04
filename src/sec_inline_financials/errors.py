class ExplorerError(Exception):
    """Base class for errors that can be presented directly at the CLI."""


class DiscoveryError(ExplorerError):
    """SEC company or filing discovery could not satisfy the request."""


class ProcessingError(ExplorerError):
    """Arelle could not produce a usable annual result."""
