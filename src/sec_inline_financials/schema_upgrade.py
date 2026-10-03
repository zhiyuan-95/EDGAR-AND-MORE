from __future__ import annotations

from collections.abc import Callable

from sec_inline_financials.errors import SchemaError
from sec_inline_financials.storage.evidence_store import EvidenceStore
from sec_inline_financials.storage.migrations import inspect_database_schema

InputFn = Callable[[str], str]
OutputFn = Callable[[str], None]


def ensure_schema_upgrade_approved(
    store: EvidenceStore,
    *,
    input_fn: InputFn = input,
    output_fn: OutputFn = print,
) -> bool:
    """Apply pending migrations only after an operator approves an existing DB upgrade."""
    schema = inspect_database_schema(store.database)

    if schema.newer_than_code:
        raise SchemaError(
            f"Database schema version {schema.current_version} is newer than "
            f"supported version {schema.supported_version}."
        )

    if not schema.database_exists or schema.current_version == 0 or not schema.pending:
        return True

    output_fn(
        f"Database schema version {schema.current_version} must be upgraded "
        f"to version {schema.supported_version}."
    )
    output_fn("Pending migrations:")
    for filename in schema.pending_filenames:
        output_fn(f"  - {filename}")
    output_fn("Warning: older code may reject the upgraded database.")

    while True:
        try:
            answer = input_fn("Apply these database migrations? [y/n]: ").strip().lower()
        except EOFError:
            answer = ""
        except KeyboardInterrupt:
            output_fn("\nMigration cancelled; database unchanged.")
            raise

        if answer == "y":
            store.initialize(allow_schema_upgrade=True)
            output_fn(f"Database upgraded to version {schema.supported_version}.")
            return True

        if answer in {"", "n"}:
            output_fn("Migration cancelled; database unchanged.")
            return False

        output_fn("Please enter y or n.")
