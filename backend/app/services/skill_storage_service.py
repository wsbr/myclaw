"""Immutable Skill package storage backends.

The database is the Skill control plane.  Package bytes live behind this
interface so a local UAT deployment and a multi-node S3 deployment share the
same upload, checksum, and cache semantics.
"""

from __future__ import annotations

import mimetypes
import hashlib
import os
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Protocol
from urllib.parse import urlparse

from app.config import settings


class SkillStorageError(RuntimeError):
    pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


class SkillPackageStore(Protocol):
    def put_file(
        self, source: Path, *, skill_id: str, version_id: str,
        checksum: str, suffix: str,
    ) -> str: ...

    def copy_to(self, uri: str, destination: Path) -> None: ...

    def exists(self, uri: str) -> bool: ...

    def delete(self, uri: str) -> None: ...


def _safe_relative_uri(uri: str) -> PurePosixPath:
    if not uri.startswith("skill-package://"):
        raise SkillStorageError("unsupported local Skill package URI")
    raw = uri.removeprefix("skill-package://")
    relative = PurePosixPath(raw)
    if not raw or relative.is_absolute() or ".." in relative.parts:
        raise SkillStorageError("unsafe local Skill package URI")
    return relative


class LocalSkillPackageStore:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root or settings.skill_package_root)

    def put_file(
        self, source: Path, *, skill_id: str, version_id: str,
        checksum: str, suffix: str,
    ) -> str:
        if _sha256_file(source) != checksum:
            raise SkillStorageError("Skill package source checksum mismatch")
        directory = self.root / skill_id / version_id
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{checksum}{suffix}"
        if target.is_file():
            if _sha256_file(target) != checksum:
                raise SkillStorageError(
                    "immutable local Skill package key contains different bytes"
                )
            return f"skill-package://{skill_id}/{version_id}/{target.name}"
        handle, staged_name = tempfile.mkstemp(
            prefix=f".{checksum}.", suffix=".tmp", dir=directory,
        )
        os.close(handle)
        staged = Path(staged_name)
        try:
            shutil.copyfile(source, staged)
            with staged.open("rb") as copied:
                os.fsync(copied.fileno())
            os.replace(staged, target)
        except OSError as error:
            raise SkillStorageError("failed to persist the Skill package") from error
        finally:
            staged.unlink(missing_ok=True)
        return f"skill-package://{skill_id}/{version_id}/{target.name}"

    def path_for(self, uri: str) -> Path:
        relative = _safe_relative_uri(uri)
        root = self.root.resolve()
        candidate = root.joinpath(*relative.parts).resolve(strict=False)
        try:
            candidate.relative_to(root)
        except ValueError as error:
            raise SkillStorageError("local Skill package URI escapes its root") from error
        return candidate

    def copy_to(self, uri: str, destination: Path) -> None:
        source = self.path_for(uri)
        if not source.is_file():
            raise SkillStorageError("Skill package source is missing")
        try:
            shutil.copyfile(source, destination)
        except OSError as error:
            raise SkillStorageError("failed to read the Skill package") from error

    def exists(self, uri: str) -> bool:
        return self.path_for(uri).is_file()

    def delete(self, uri: str) -> None:
        self.path_for(uri).unlink(missing_ok=True)


class S3SkillPackageStore:
    def __init__(self, *, client=None) -> None:
        self.bucket = settings.skill_s3_bucket
        if client is not None:
            self.client = client
            return
        try:
            import boto3
            from botocore.config import Config
        except ImportError as error:  # pragma: no cover - deployment dependency
            raise SkillStorageError("boto3 is required for S3 Skill storage") from error
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.skill_s3_endpoint_url,
            region_name=settings.skill_s3_region,
            aws_access_key_id=settings.skill_s3_access_key_id,
            aws_secret_access_key=settings.skill_s3_secret_access_key,
            config=Config(
                s3={"addressing_style": "path" if settings.skill_s3_force_path_style else "auto"},
            ),
        )

    def _location(self, uri: str) -> tuple[str, str]:
        parsed = urlparse(uri)
        if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.lstrip("/"):
            raise SkillStorageError("invalid S3 Skill package URI")
        if parsed.netloc != self.bucket:
            raise SkillStorageError("S3 Skill package URI is outside the configured bucket")
        return parsed.netloc, parsed.path.lstrip("/")

    def put_file(
        self, source: Path, *, skill_id: str, version_id: str,
        checksum: str, suffix: str,
    ) -> str:
        if _sha256_file(source) != checksum:
            raise SkillStorageError("Skill package source checksum mismatch")
        key = f"skills/{skill_id}/{version_id}/{checksum}{suffix}"
        uri = f"s3://{self.bucket}/{key}"
        content_type = mimetypes.guess_type(f"package{suffix}")[0] or "application/octet-stream"
        try:
            # The application role can create a content-addressed object once,
            # but an upload retry must never replace bytes already stored for
            # the immutable SkillVersion key.
            with source.open("rb") as body:
                self.client.put_object(
                    Bucket=self.bucket,
                    Key=key,
                    Body=body,
                    ContentType=content_type,
                    Metadata={
                        "sha256": checksum,
                        "skill-id": skill_id,
                        "version-id": version_id,
                    },
                    IfNoneMatch="*",
                )
        except Exception as error:  # boto3 exposes several transport-specific types
            code = str(
                getattr(error, "response", {}).get("Error", {}).get("Code", "")
            )
            if code in {"409", "412", "ConditionalRequestConflict", "PreconditionFailed"}:
                try:
                    existing = self.client.head_object(Bucket=self.bucket, Key=key)
                except Exception as head_error:
                    raise SkillStorageError(
                        "failed to verify the existing immutable Skill package"
                    ) from head_error
                metadata = existing.get("Metadata") or {}
                if (
                    metadata.get("sha256") == checksum
                    and int(existing.get("ContentLength", -1)) == source.stat().st_size
                ):
                    return uri
                raise SkillStorageError(
                    "immutable S3 Skill package key contains different bytes"
                ) from error
            raise SkillStorageError("failed to upload the Skill package to object storage") from error
        return uri

    def copy_to(self, uri: str, destination: Path) -> None:
        bucket, key = self._location(uri)
        try:
            self.client.download_file(bucket, key, str(destination))
        except Exception as error:
            raise SkillStorageError("failed to download the Skill package from object storage") from error

    def exists(self, uri: str) -> bool:
        bucket, key = self._location(uri)
        try:
            self.client.head_object(Bucket=bucket, Key=key)
            return True
        except Exception as error:
            code = str(
                getattr(error, "response", {}).get("Error", {}).get("Code", "")
            )
            if code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise SkillStorageError(
                "failed to check the Skill package in object storage"
            ) from error

    def delete(self, uri: str) -> None:
        bucket, key = self._location(uri)
        try:
            self.client.delete_object(Bucket=bucket, Key=key)
        except Exception as error:
            raise SkillStorageError("failed to delete the Skill package from object storage") from error


def get_skill_package_store() -> SkillPackageStore:
    if settings.skill_storage_backend == "local":
        return LocalSkillPackageStore()
    if settings.skill_storage_backend == "s3":
        return S3SkillPackageStore()
    raise SkillStorageError(f"unsupported Skill storage backend: {settings.skill_storage_backend}")
