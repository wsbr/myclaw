"""Portable Skill package validation and synchronization tests."""

import io
import zipfile

import pytest
from starlette.datastructures import Headers, UploadFile

from app.config import settings
from app.services.skill_package_service import (
    InvalidSkillPackage, receive_and_validate, store_package, sync_package,
)

pytestmark = pytest.mark.unit


def archive(entries: dict[str, str]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as package:
        for name, content in entries.items():
            package.writestr(name, content)
    return output.getvalue()


def upload(payload: bytes) -> UploadFile:
    return UploadFile(filename="skill.zip", file=io.BytesIO(payload), headers=Headers())


@pytest.mark.asyncio
async def test_valid_skill_is_content_addressed_and_synced_read_only(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "skill_package_root", str(tmp_path / "packages"))
    monkeypatch.setattr(settings, "shared_skill_root", str(tmp_path / "shared"))
    payload = archive({
        "SKILL.md": "---\nname: Writer\ndescription: Draft clear prose\n---\n# Instructions",
        "references/style.md": "Prefer concise sentences.",
    })
    temporary, checksum, name, description, suffix = await receive_and_validate(upload(payload))
    assert name == "Writer"
    assert description == "Draft clear prose"
    uri = store_package(temporary, "skill-1", "version-1", suffix)
    path_uri = sync_package("skill-1", checksum, uri)
    assert path_uri == f"skill://skill-1/{checksum}/"
    manifest = tmp_path / "shared" / "skill-1" / checksum / "SKILL.md"
    assert manifest.is_file()
    assert manifest.stat().st_mode & 0o222 == 0


@pytest.mark.asyncio
async def test_skill_archive_rejects_parent_traversal():
    payload = archive({
        "SKILL.md": "name: Safe\ndescription: Safe package",
        "../escape.txt": "escape",
    })
    with pytest.raises(InvalidSkillPackage, match="unsafe archive path"):
        await receive_and_validate(upload(payload))
