"""Portable Skill archive validation and content-addressed synchronization."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from fastapi import UploadFile

from app.config import settings


class InvalidSkillPackage(ValueError):
    pass


def _safe_name(name: str) -> str:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if not normalized or normalized.startswith("/") or ".." in path.parts:
        raise InvalidSkillPackage(f"unsafe archive path: {name}")
    return normalized


def _manifest_fields(content: str) -> tuple[str, str]:
    header = content
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            header = parts[1]
    values: dict[str, str] = {}
    for line in header.splitlines():
        match = re.match(r"^(name|description)\s*:\s*(.+?)\s*$", line, re.I)
        if match:
            values[match.group(1).lower()] = match.group(2).strip().strip('"\'')
    if not values.get("name") or not values.get("description"):
        raise InvalidSkillPackage("SKILL.md must declare name and description")
    return values["name"], values["description"]


async def receive_and_validate(upload: UploadFile) -> tuple[Path, str, str, str, str]:
    filename = upload.filename or "skill.zip"
    lower = filename.lower()
    if not (lower.endswith(".zip") or lower.endswith(".tar.gz") or lower.endswith(".tgz")):
        raise InvalidSkillPackage("only .zip and .tar.gz packages are accepted")
    suffix = ".zip" if lower.endswith(".zip") else ".tar.gz"
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
        if suffix == ".zip":
            with zipfile.ZipFile(temp_path) as archive:
                members = archive.infolist()
                if len(members) > settings.skill_max_files:
                    raise InvalidSkillPackage("Skill package has too many files")
                total = 0
                manifest = None
                for member in members:
                    name = _safe_name(member.filename)
                    mode = member.external_attr >> 16
                    if stat.S_ISLNK(mode):
                        raise InvalidSkillPackage("archive links are not accepted")
                    total += member.file_size
                    if total > settings.skill_max_unpacked_bytes:
                        raise InvalidSkillPackage("Skill package unpacked size exceeds limit")
                    if name == "SKILL.md":
                        manifest = archive.read(member).decode("utf-8")
        else:
            with tarfile.open(temp_path, "r:gz") as archive:
                members = archive.getmembers()
                if len(members) > settings.skill_max_files:
                    raise InvalidSkillPackage("Skill package has too many files")
                total = 0
                manifest = None
                for member in members:
                    name = _safe_name(member.name)
                    if member.issym() or member.islnk() or member.isdev():
                        raise InvalidSkillPackage("archive links and device files are not accepted")
                    total += member.size
                    if total > settings.skill_max_unpacked_bytes:
                        raise InvalidSkillPackage("Skill package unpacked size exceeds limit")
                    if name == "SKILL.md" and member.isfile():
                        extracted = archive.extractfile(member)
                        manifest = extracted.read().decode("utf-8") if extracted else None
        if manifest is None:
            raise InvalidSkillPackage("Skill package root must contain SKILL.md")
        name, description = _manifest_fields(manifest)
        return temp_path, digest.hexdigest(), name, description, suffix
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def store_package(temp_path: Path, skill_id: str, version_id: str, suffix: str) -> str:
    directory = Path(settings.skill_package_root) / skill_id
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{version_id}{suffix}"
    # Uploads normally land in /tmp while the package root may be a mounted
    # volume. os.replace cannot cross filesystems, so stage inside the target
    # directory and only then perform the atomic rename.
    handle, staged_name = tempfile.mkstemp(prefix=f".{version_id}.", suffix=".tmp", dir=directory)
    os.close(handle)
    staged = Path(staged_name)
    try:
        shutil.copyfile(temp_path, staged)
        with staged.open("rb") as copied:
            os.fsync(copied.fileno())
        os.replace(staged, target)
    finally:
        staged.unlink(missing_ok=True)
        temp_path.unlink(missing_ok=True)
    return f"skill-package://{skill_id}/{target.name}"


def package_path(uri: str) -> Path:
    if not uri.startswith("skill-package://"):
        raise InvalidSkillPackage("unsupported package URI")
    relative = PurePosixPath(uri.removeprefix("skill-package://"))
    if ".." in relative.parts:
        raise InvalidSkillPackage("unsafe package URI")
    return Path(settings.skill_package_root).joinpath(*relative.parts)


def sync_package(skill_id: str, checksum: str, uri: str) -> str:
    source = package_path(uri)
    if not source.is_file():
        raise InvalidSkillPackage("Skill package source is missing")
    actual = hashlib.sha256(source.read_bytes()).hexdigest()
    if actual != checksum:
        raise InvalidSkillPackage("Skill package checksum mismatch")
    parent = Path(settings.shared_skill_root) / skill_id
    target = parent / checksum
    if target.is_dir():
        return f"skill://{skill_id}/{checksum}/"
    parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f"{checksum}.tmp.", dir=parent))
    try:
        if source.name.endswith(".zip"):
            with zipfile.ZipFile(source) as archive:
                archive.extractall(temporary)
        else:
            with tarfile.open(source, "r:gz") as archive:
                archive.extractall(temporary, filter="data")
        for path in temporary.rglob("*"):
            path.chmod(0o555 if path.is_dir() else 0o444)
        os.replace(temporary, target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return f"skill://{skill_id}/{checksum}/"
