from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sec_inline_financials.errors import DatabaseBusyError, EvidenceStorageError


class EvidenceDatabase:
    """Own SQLite connection configuration and transaction boundaries."""

    def __init__(self, path: Path, *, busy_timeout_seconds: float = 5.0) -> None:
        self.path = path
        self._busy_timeout_seconds = busy_timeout_seconds

    def connect(self) -> sqlite3.Connection:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                self.path,
                timeout=self._busy_timeout_seconds,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {int(self._busy_timeout_seconds * 1000)}")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            return connection
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise DatabaseBusyError(f"Evidence database is busy: {self.path}") from exc
            raise EvidenceStorageError(
                f"Could not open evidence database {self.path}: {exc}"
            ) from exc

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    def connect_read_only(self) -> sqlite3.Connection:
        """Open an existing database without creating or reconfiguring it."""
        try:
            uri = f"{self.path.resolve().as_uri()}?mode=ro"
            connection = sqlite3.connect(
                uri,
                uri=True,
                timeout=self._busy_timeout_seconds,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            connection.execute(f"PRAGMA busy_timeout = {int(self._busy_timeout_seconds * 1000)}")
            return connection
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise DatabaseBusyError(f"Evidence database is busy: {self.path}") from exc
            raise EvidenceStorageError(
                f"Could not open evidence database read-only: {self.path}: {exc}"
            ) from exc

    @contextmanager
    def read_connection(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect_read_only()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def write_transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.execute("COMMIT")
            except sqlite3.OperationalError as exc:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                    raise DatabaseBusyError(f"Evidence database is busy: {self.path}") from exc
                raise
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
