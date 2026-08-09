"""Content-addressed, disposable local cache for immutable Skill packages."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from app.config import settings
from app.services.skill_storage_service import SkillStorageError, get_skill_package_store


class SkillCacheError(RuntimeError):
    pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if relative == ".myclaw-skill.json":
            continue
        if path.is_symlink():
            raise SkillCacheError("Skill cache contains a symbolic link")
        digest.update(("d:" if path.is_dir() else "f:").encode("ascii"))
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        if path.is_file():
            with path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
    return digest.hexdigest()


def _archive_suffix(uri: str) -> str:
    lowered = uri.casefold()
    if lowered.endswith(".zip"):
        return ".zip"
    if lowered.endswith(".tar.gz") or lowered.endswith(".tgz"):
        return ".tar.gz"
    raise SkillCacheError("Skill package URI has an unsupported archive type")


def _make_writable(root: Path) -> None:
    if not root.exists() or root.is_symlink():
        return
    for path in root.rglob("*"):
        if not path.is_symlink():
            path.chmod(0o755 if path.is_dir() else 0o644)
    root.chmod(0o755)


class SkillCacheService:
    def __init__(self, root: str | Path | None = None, *, store=None) -> None:
        self.root = Path(root or settings.shared_skill_root)
        self.store = store or get_skill_package_store()

    def cache_path(self, skill_id: str, checksum: str) -> Path:
        return self.root / skill_id / checksum

    @staticmethod
    def path_uri(skill_id: str, checksum: str) -> str:
        return f"skill://{skill_id}/{checksum}/"

    def _valid(self, target: Path, *, skill_id: str, checksum: str, package_uri: str) -> bool:
        if target.is_symlink() or not (target / "SKILL.md").is_file():
            return False
        marker = target / ".myclaw-skill.json"
        try:
            value = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        expected = {
            "schema_version": "skill-cache.v1",
            "skill_id": skill_id,
            "checksum": checksum,
            "package_uri": package_uri,
        }
        if any(value.get(key) != item for key, item in expected.items()):
            return False
        try:
            if value.get("tree_hash") != _tree_hash(target):
                return False
            return not (target.stat().st_mode & 0o222) and all(
                path.is_symlink() or not (path.stat().st_mode & 0o222)
                for path in target.rglob("*")
            )
        except (OSError, SkillCacheError):
            return False

    def ensure(self, *, skill_id: str, checksum: str, package_uri: str) -> str:
        target = self.cache_path(skill_id, checksum)
        if self._valid(target, skill_id=skill_id, checksum=checksum, package_uri=package_uri):
            return self.path_uri(skill_id, checksum)

        parent = target.parent
        parent.mkdir(parents=True, exist_ok=True)
        locks = self.root / ".locks"
        locks.mkdir(parents=True, exist_ok=True)
        lock_path = locks / f"{skill_id}-{checksum}.lock"
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if self._valid(target, skill_id=skill_id, checksum=checksum, package_uri=package_uri):
                return self.path_uri(skill_id, checksum)
            package_handle, package_name = tempfile.mkstemp(
                prefix=f".{checksum}.", suffix=_archive_suffix(package_uri), dir=parent,
            )
            os.close(package_handle)
            package = Path(package_name)
            temporary = Path(tempfile.mkdtemp(prefix=f".{checksum}.extract.", dir=parent))
            try:
                self.store.copy_to(package_uri, package)
                if _sha256_file(package) != checksum:
                    raise SkillCacheError("Skill package checksum mismatch")
                from app.services.skill_package_service import extract_validated_archive

                extract_validated_archive(package, _archive_suffix(package_uri), temporary)
                tree_hash = _tree_hash(temporary)
                marker = temporary / ".myclaw-skill.json"
                marker.write_text(json.dumps({
                    "schema_version": "skill-cache.v1",
                    "skill_id": skill_id,
                    "checksum": checksum,
                    "package_uri": package_uri,
                    "tree_hash": tree_hash,
                }, ensure_ascii=False, sort_keys=True), encoding="utf-8")
                for path in temporary.rglob("*"):
                    if path.is_symlink():
                        raise SkillCacheError("Skill cache contains a symbolic link")
                    path.chmod(0o555 if path.is_dir() else 0o444)
                if target.exists() or target.is_symlink():
                    _make_writable(target)
                    if target.is_dir() and not target.is_symlink():
                        shutil.rmtree(target)
                    else:
                        target.unlink(missing_ok=True)
                os.replace(temporary, target)
                target.chmod(0o555)
            except (SkillCacheError, SkillStorageError):
                raise
            except Exception as error:
                raise SkillCacheError(str(error)) from error
            finally:
                package.unlink(missing_ok=True)
                if temporary.exists():
                    _make_writable(temporary)
                    shutil.rmtree(temporary, ignore_errors=True)
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return self.path_uri(skill_id, checksum)
