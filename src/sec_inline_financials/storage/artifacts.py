from __future__ import annotations

import hashlib
import os
import re
import shutil
from contextlib import suppress
from pathlib import Path

from sec_inline_financials.errors import (
    ArtifactError,
    ArtifactHashMismatchError,
    MissingArtifactError,
)
from sec_inline_financials.evidence_models import InstalledArtifact

_SAFE_SEGMENT = re.compile(r"[A-Za-z0-9._-]+")


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
    except OSError as exc:
        raise ArtifactError(f"Could not read artifact source {path}: {exc}") from exc
    return digest.hexdigest(), size


class ArtifactStore:
    """Install and verify immutable, content-addressed evidence files."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.objects_root = root / "objects" / "sha256"
        self.staging_root = root / "staging"

    def stage_directory(self, attempt_identity: str) -> Path:
        if _SAFE_SEGMENT.fullmatch(attempt_identity) is None:
            raise ArtifactError(f"Unsafe attempt identity: {attempt_identity!r}")
        path = self.staging_root / attempt_identity
        path.mkdir(parents=True, exist_ok=True)
        return path

    def stage_bytes(self, attempt_identity: str, logical_name: str, content: bytes) -> Path:
        if _SAFE_SEGMENT.fullmatch(logical_name) is None:
            raise ArtifactError(f"Unsafe artifact logical name: {logical_name!r}")
        destination = self.stage_directory(attempt_identity) / logical_name
        try:
            with destination.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            existing_hash, _size = _sha256_file(destination)
            expected_hash = hashlib.sha256(content).hexdigest()
            if existing_hash != expected_hash:
                raise ArtifactHashMismatchError(
                    f"Staged artifact {logical_name} already exists with different bytes."
                ) from None
        except OSError as exc:
            raise ArtifactError(f"Could not stage artifact {logical_name}: {exc}") from exc
        return destination

    def stage_file(self, attempt_identity: str, logical_name: str, source: Path) -> Path:
        if _SAFE_SEGMENT.fullmatch(logical_name) is None:
            raise ArtifactError(f"Unsafe artifact logical name: {logical_name!r}")
        destination = self.stage_directory(attempt_identity) / logical_name
        try:
            with source.open("rb") as source_stream, destination.open("xb") as destination_stream:
                shutil.copyfileobj(source_stream, destination_stream)
                destination_stream.flush()
                os.fsync(destination_stream.fileno())
        except FileExistsError:
            source_hash, source_size = _sha256_file(source)
            destination_hash, destination_size = _sha256_file(destination)
            if (source_hash, source_size) != (destination_hash, destination_size):
                raise ArtifactHashMismatchError(
                    f"Staged artifact {logical_name} already exists with different bytes."
                ) from None
        except OSError as exc:
            raise ArtifactError(f"Could not stage {source} as {logical_name}: {exc}") from exc
        return destination

    def install(
        self,
        staged_path: Path,
        *,
        purpose: str,
        logical_name: str,
        media_type: str = "application/octet-stream",
        source_document_key: str | None = None,
    ) -> InstalledArtifact:
        digest, size = _sha256_file(staged_path)
        relative = Path("objects") / "sha256" / digest[:2] / digest
        destination = self.root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            self._verify_path(destination, digest, size)
        else:
            temporary = destination.with_name(f".{digest}.{os.getpid()}.tmp")
            try:
                with staged_path.open("rb") as source, temporary.open("xb") as target:
                    shutil.copyfileobj(source, target)
                    target.flush()
                    os.fsync(target.fileno())
                try:
                    os.link(temporary, destination)
                except FileExistsError:
                    self._verify_path(destination, digest, size)
            except OSError as exc:
                raise ArtifactError(f"Could not install artifact {logical_name}: {exc}") from exc
            finally:
                with suppress(OSError):
                    temporary.unlink(missing_ok=True)
        return InstalledArtifact(
            sha256=digest,
            relative_object_path=relative.as_posix(),
            byte_size=size,
            media_type=media_type,
            purpose=purpose,
            logical_name=logical_name,
            source_document_key=source_document_key,
        )

    def resolve(self, relative_object_path: str, sha256: str, byte_size: int) -> Path:
        relative = Path(relative_object_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ArtifactError(f"Unsafe artifact path: {relative_object_path}")
        root = self.root.resolve()
        path = (self.root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ArtifactError(
                f"Artifact path escapes the configured root: {relative_object_path}"
            ) from exc
        if not path.is_file():
            raise MissingArtifactError(f"Retained artifact is missing: {relative_object_path}")
        self._verify_path(path, sha256, byte_size)
        return path

    def cleanup_staging(self, attempt_identity: str) -> None:
        if _SAFE_SEGMENT.fullmatch(attempt_identity) is None:
            raise ArtifactError(f"Unsafe attempt identity: {attempt_identity!r}")
        target = (self.staging_root / attempt_identity).resolve()
        staging_root = self.staging_root.resolve()
        try:
            target.relative_to(staging_root)
        except ValueError as exc:
            raise ArtifactError("Attempt staging path escapes the configured root.") from exc
        if target == staging_root:
            raise ArtifactError("Refusing to remove the staging root itself.")
        if target.exists():
            shutil.rmtree(target)

    @staticmethod
    def _verify_path(path: Path, expected_hash: str, expected_size: int) -> None:
        actual_hash, actual_size = _sha256_file(path)
        if actual_hash != expected_hash or actual_size != expected_size:
            raise ArtifactHashMismatchError(
                f"Artifact integrity failure for {path}: expected {expected_hash}/{expected_size}, "
                f"found {actual_hash}/{actual_size}."
            )
