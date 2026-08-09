"""Portable Skill archive validation and content-addressed synchronization."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml
from fastapi import UploadFile

from app.config import settings
from app.services.skill_storage_service import (
    LocalSkillPackageStore,
    SkillStorageError,
    get_skill_package_store,
)


class InvalidSkillPackage(ValueError):
    pass


class _NoAliasSafeLoader(yaml.SafeLoader):
    """Safe YAML loader that also rejects alias-expansion payloads."""

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise yaml.YAMLError("YAML aliases are not accepted in SKILL.md")
        return super().compose_node(parent, index)


@dataclass(frozen=True)
class ValidatedSkillPackage:
    path: Path
    checksum: str
    name: str
    description: str
    suffix: str
    package_size: int
    file_count: int
    unpacked_size: int
    declared_metadata: dict

    def __iter__(self):
        """Keep the original five-value destructuring API for callers/tests."""

        yield self.path
        yield self.checksum
        yield self.name
        yield self.description
        yield self.suffix

    @property
    def manifest(self) -> dict:
        return {
            "schema_version": "skill.manifest.v1",
            "name": self.name,
            "description": self.description,
            "archive_type": self.suffix.removeprefix("."),
            "package_size": self.package_size,
            "file_count": self.file_count,
            "unpacked_size": self.unpacked_size,
            "declared_metadata": self.declared_metadata,
        }


def _safe_name(name: str) -> str:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if not normalized or normalized.startswith("/") or ".." in path.parts:
        raise InvalidSkillPackage(f"unsafe archive path: {name}")
    return normalized


def _manifest_fields(content: str) -> tuple[str, str, dict]:
    header = content
    metadata: dict = {}
    if re.match(r"\A---[ \t]*(?:\r?\n|\Z)", content):
        match = re.match(
            r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)",
            content,
            re.DOTALL,
        )
        if match is None:
            raise InvalidSkillPackage("SKILL.md YAML front matter is not closed")
        header = match.group(1)
        try:
            parsed = yaml.load(header, Loader=_NoAliasSafeLoader) or {}
            if not isinstance(parsed, dict):
                raise InvalidSkillPackage("SKILL.md front matter must be a YAML object")
            metadata = json.loads(json.dumps(parsed, ensure_ascii=False, default=str))
        except yaml.YAMLError as error:
            raise InvalidSkillPackage(
                "SKILL.md YAML front matter is invalid or uses aliases"
            ) from error
        except (RecursionError, TypeError, ValueError) as error:
            raise InvalidSkillPackage("SKILL.md metadata is not safely serializable") from error
    if not metadata:
        for line in header.splitlines():
            match = re.match(r"^(name|description)\s*:\s*(.+?)\s*$", line, re.I)
            if match:
                metadata[match.group(1).lower()] = match.group(2).strip().strip('"\'')
    name = metadata.get("name")
    description = metadata.get("description")
    if not isinstance(name, str) or not name.strip() or not isinstance(description, str) or not description.strip():
        raise InvalidSkillPackage("SKILL.md must declare name and description")
    return name.strip(), description.strip(), metadata


def inspect_archive(path: Path, suffix: str) -> tuple[str, int, int]:
    """Validate archive structure and return SKILL.md, file count, total size."""

    if suffix == ".zip":
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > settings.skill_max_files:
                raise InvalidSkillPackage("Skill package has too many files")
            total = 0
            manifest = None
            seen: set[str] = set()
            for member in members:
                name = _safe_name(member.filename)
                if name in seen:
                    raise InvalidSkillPackage(f"duplicate archive path: {name}")
                seen.add(name)
                mode = member.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise InvalidSkillPackage("archive links are not accepted")
                if stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                    raise InvalidSkillPackage("archive special files are not accepted")
                total += member.file_size
                if total > settings.skill_max_unpacked_bytes:
                    raise InvalidSkillPackage("Skill package unpacked size exceeds limit")
                if name == "SKILL.md":
                    if member.file_size > settings.skill_manifest_max_bytes:
                        raise InvalidSkillPackage("SKILL.md exceeds metadata size limit")
                    manifest = archive.read(member).decode("utf-8")
    else:
        with tarfile.open(path, "r:gz") as archive:
            members = archive.getmembers()
            if len(members) > settings.skill_max_files:
                raise InvalidSkillPackage("Skill package has too many files")
            total = 0
            manifest = None
            seen = set()
            for member in members:
                name = _safe_name(member.name)
                if name in seen:
                    raise InvalidSkillPackage(f"duplicate archive path: {name}")
                seen.add(name)
                if member.issym() or member.islnk() or member.isdev():
                    raise InvalidSkillPackage("archive links and device files are not accepted")
                total += member.size
                if total > settings.skill_max_unpacked_bytes:
                    raise InvalidSkillPackage("Skill package unpacked size exceeds limit")
                if name == "SKILL.md" and member.isfile():
                    if member.size > settings.skill_manifest_max_bytes:
                        raise InvalidSkillPackage("SKILL.md exceeds metadata size limit")
                    extracted = archive.extractfile(member)
                    manifest = extracted.read().decode("utf-8") if extracted else None
    if manifest is None:
        raise InvalidSkillPackage("Skill package root must contain SKILL.md")
    return manifest, len(members), total


def extract_validated_archive(source: Path, suffix: str, destination: Path) -> None:
    """Revalidate and safely extract bytes fetched from the package store."""

    inspect_archive(source, suffix)
    if suffix == ".zip":
        with zipfile.ZipFile(source) as archive:
            archive.extractall(destination)
    else:
        with tarfile.open(source, "r:gz") as archive:
            archive.extractall(destination, filter="data")


def _validated_package_file(path: Path, suffix: str) -> ValidatedSkillPackage:
    """Validate a package already present on disk and describe its manifest."""

    package_size = path.stat().st_size
    if package_size > settings.skill_max_package_bytes:
        raise InvalidSkillPackage("Skill package exceeds size limit")
    manifest, file_count, unpacked_size = inspect_archive(path, suffix)
    name, description, declared_metadata = _manifest_fields(manifest)
    return ValidatedSkillPackage(
        path=path,
        checksum=_sha256_file(path),
        name=name,
        description=description,
        suffix=suffix,
        package_size=package_size,
        file_count=file_count,
        unpacked_size=unpacked_size,
        declared_metadata=declared_metadata,
    )


def _safe_markdown_path(value: str) -> str:
    normalized = _safe_name(value.strip())
    if PurePosixPath(normalized).suffix.lower() != ".md":
        raise InvalidSkillPackage("only Markdown files can be edited")
    return normalized


def _copy_verified_package(
    package_uri: str, checksum: str, suffix: str, destination: Path,
) -> None:
    get_skill_package_store().copy_to(package_uri, destination)
    if _sha256_file(destination) != checksum:
        raise InvalidSkillPackage("Skill package checksum mismatch")
    inspect_archive(destination, suffix)


def read_package_markdown(
    *, package_uri: str, checksum: str, suffix: str, requested_path: str = "SKILL.md",
) -> dict:
    """List Markdown files and read one file from an immutable package."""

    requested = _safe_markdown_path(requested_path)
    with tempfile.TemporaryDirectory(prefix="myclaw-skill-view-") as temp_name:
        root = Path(temp_name)
        source = root / f"package{suffix}"
        extracted = root / "content"
        extracted.mkdir()
        _copy_verified_package(package_uri, checksum, suffix, source)
        extract_validated_archive(source, suffix, extracted)

        files = []
        for path in sorted(extracted.rglob("*")):
            if not path.is_file() or path.suffix.lower() != ".md":
                continue
            relative = path.relative_to(extracted).as_posix()
            files.append({"path": relative, "size": path.stat().st_size})

        target = extracted.joinpath(*PurePosixPath(requested).parts)
        if not target.is_file() or target.suffix.lower() != ".md":
            raise InvalidSkillPackage("Markdown file not found in Skill package")
        if target.stat().st_size > settings.skill_manifest_max_bytes:
            raise InvalidSkillPackage("Markdown file exceeds editor size limit")
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeError as error:
            raise InvalidSkillPackage("Markdown file must be valid UTF-8") from error
        return {"files": files, "path": requested, "content": content}


def _write_deterministic_zip(source: Path, destination: Path) -> None:
    with zipfile.ZipFile(
        destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9,
    ) as archive:
        for path in sorted(source.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(source).as_posix()
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, path.read_bytes())


def build_markdown_revision(
    *, package_uri: str, checksum: str, suffix: str, path: str, content: str,
) -> ValidatedSkillPackage:
    """Create a new validated package by editing one Markdown file.

    The source package is never mutated. The caller persists the returned
    temporary ZIP as a new immutable SkillVersion.
    """

    requested = _safe_markdown_path(path)
    encoded = content.encode("utf-8")
    if len(encoded) > settings.skill_manifest_max_bytes:
        raise InvalidSkillPackage("Markdown file exceeds editor size limit")

    with tempfile.TemporaryDirectory(prefix="myclaw-skill-edit-") as temp_name:
        root = Path(temp_name)
        source = root / f"package{suffix}"
        extracted = root / "content"
        extracted.mkdir()
        _copy_verified_package(package_uri, checksum, suffix, source)
        extract_validated_archive(source, suffix, extracted)

        target = extracted.joinpath(*PurePosixPath(requested).parts)
        if not target.is_file() or target.suffix.lower() != ".md":
            raise InvalidSkillPackage("Markdown file not found in Skill package")
        try:
            current = target.read_text(encoding="utf-8")
        except UnicodeError as error:
            raise InvalidSkillPackage("Markdown file must be valid UTF-8") from error
        if current == content:
            raise InvalidSkillPackage("Markdown content is unchanged")
        target.write_text(content, encoding="utf-8")

        handle, output_name = tempfile.mkstemp(
            prefix="myclaw-skill-revision-", suffix=".zip",
        )
        os.close(handle)
        output = Path(output_name)
        try:
            _write_deterministic_zip(extracted, output)
            return _validated_package_file(output, ".zip")
        except Exception:
            output.unlink(missing_ok=True)
            raise


async def receive_and_validate(upload: UploadFile) -> ValidatedSkillPackage:
    filename = upload.filename or "skill.zip"
    lower = filename.lower()
    if not (
        lower.endswith(".zip") or lower.endswith(".skill")
        or lower.endswith(".tar.gz") or lower.endswith(".tgz")
    ):
        raise InvalidSkillPackage("only .skill, .zip, and .tar.gz packages are accepted")
    suffix = ".zip" if lower.endswith((".zip", ".skill")) else ".tar.gz"
    handle, temp_name = tempfile.mkstemp(prefix="myclaw-skill-", suffix=suffix)
    os.close(handle)
    temp_path = Path(temp_name)
    digest = hashlib.sha256()
    size = 0
    try:
        with temp_path.open("wb") as destination:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > settings.skill_max_package_bytes:
                    raise InvalidSkillPackage("Skill package exceeds size limit")
                digest.update(chunk)
                destination.write(chunk)
        manifest, file_count, unpacked_size = inspect_archive(temp_path, suffix)
        name, description, declared_metadata = _manifest_fields(manifest)
        return ValidatedSkillPackage(
            path=temp_path,
            checksum=digest.hexdigest(),
            name=name,
            description=description,
            suffix=suffix,
            package_size=size,
            file_count=file_count,
            unpacked_size=unpacked_size,
            declared_metadata=declared_metadata,
        )
    except InvalidSkillPackage:
        temp_path.unlink(missing_ok=True)
        raise
    except (
        OSError, UnicodeError, tarfile.TarError, zipfile.BadZipFile, yaml.YAMLError,
    ) as error:
        temp_path.unlink(missing_ok=True)
        raise InvalidSkillPackage("Skill package is not a valid readable archive") from error


def store_package(
    temp_path: Path, skill_id: str, version_id: str, suffix: str,
    checksum: str | None = None,
) -> str:
    checksum = checksum or _sha256_file(temp_path)
    try:
        return get_skill_package_store().put_file(
            temp_path,
            skill_id=skill_id,
            version_id=version_id,
            checksum=checksum,
            suffix=suffix,
        )
    finally:
        temp_path.unlink(missing_ok=True)


def package_path(uri: str) -> Path:
    try:
        return LocalSkillPackageStore().path_for(uri)
    except SkillStorageError as error:
        raise InvalidSkillPackage(str(error)) from error


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def skill_version_content_hash(
    *, skill_id: str, version_id: str, checksum: str,
    package_uri: str, manifest: dict,
) -> str:
    payload = {
        "skill_id": skill_id,
        "version_id": version_id,
        "checksum": checksum,
        "package_uri": package_uri,
        "manifest": manifest,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def sync_package(skill_id: str, checksum: str, uri: str) -> str:
    from app.services.skill_cache_service import SkillCacheError, SkillCacheService

    try:
        return SkillCacheService().ensure(
            skill_id=skill_id, checksum=checksum, package_uri=uri,
        )
    except SkillCacheError as error:
        raise InvalidSkillPackage(str(error)) from error
