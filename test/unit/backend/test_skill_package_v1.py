"""Portable Skill package validation and synchronization tests."""

import io
import hashlib
import zipfile

import pytest
from starlette.datastructures import Headers, UploadFile

from app.config import settings
from app.services.skill_package_service import (
    InvalidSkillPackage, build_markdown_revision, read_package_markdown,
    receive_and_validate, store_package, sync_package,
)
from app.services.skill_cache_service import SkillCacheService
from app.services.skill_storage_service import S3SkillPackageStore, SkillStorageError

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

    # A content-addressed cache is disposable and self-healing. Local
    # corruption must never silently change an immutable SkillVersion.
    manifest.chmod(0o644)
    manifest.write_text("tampered", encoding="utf-8")
    sync_package("skill-1", checksum, uri)
    assert "# Instructions" in manifest.read_text(encoding="utf-8")
    assert manifest.stat().st_mode & 0o222 == 0


@pytest.mark.asyncio
async def test_skill_archive_rejects_parent_traversal():
    payload = archive({
        "SKILL.md": "name: Safe\ndescription: Safe package",
        "../escape.txt": "escape",
    })
    with pytest.raises(InvalidSkillPackage, match="unsafe archive path"):
        await receive_and_validate(upload(payload))


@pytest.mark.asyncio
async def test_skill_manifest_rejects_alias_expansion_and_oversize(monkeypatch):
    alias_payload = archive({
        "SKILL.md": (
            "---\nname: &shared Safe\ndescription: *shared\n---\n# Instructions"
        ),
    })
    with pytest.raises(InvalidSkillPackage, match="uses aliases"):
        await receive_and_validate(upload(alias_payload))

    monkeypatch.setattr(settings, "skill_manifest_max_bytes", 64)
    oversized = archive({
        "SKILL.md": "name: Safe\ndescription: " + ("x" * 128),
    })
    with pytest.raises(InvalidSkillPackage, match="metadata size limit"):
        await receive_and_validate(upload(oversized))


@pytest.mark.asyncio
async def test_skill_extension_and_yaml_metadata_are_portable():
    payload = archive({
        "SKILL.md": (
            "---\nname: portable-writer\ndescription: >-\n"
            "  Draft clear prose across\n  multiple source files.\n"
            "allowed-tools:\n  - read_file\n---\n# Workflow"
        ),
    })
    package = await receive_and_validate(UploadFile(
        filename="portable.skill", file=io.BytesIO(payload), headers=Headers(),
    ))
    try:
        assert package.suffix == ".zip"
        assert package.description == "Draft clear prose across multiple source files."
        assert package.declared_metadata["allowed-tools"] == ["read_file"]
        assert package.manifest["declared_metadata"]["name"] == "portable-writer"
    finally:
        package.path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_markdown_editor_creates_a_new_package_without_mutating_source(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(settings, "skill_package_root", str(tmp_path / "packages"))
    payload = archive({
        "SKILL.md": (
            "---\nname: writer\ndescription: Draft clear prose\n---\n"
            "# Original instructions"
        ),
        "references/style.md": "Prefer concise sentences.",
        "assets/logo.bin": "unchanged-binary",
    })
    original = await receive_and_validate(upload(payload))
    original_checksum = original.checksum
    uri = store_package(
        original.path, "skill-1", "version-1", original.suffix, original.checksum,
    )

    viewed = read_package_markdown(
        package_uri=uri,
        checksum=original_checksum,
        suffix=".zip",
        requested_path="references/style.md",
    )
    assert viewed["content"] == "Prefer concise sentences."
    assert [item["path"] for item in viewed["files"]] == [
        "SKILL.md", "references/style.md",
    ]
    assert all(item["size"] > 0 for item in viewed["files"])

    revised = build_markdown_revision(
        package_uri=uri,
        checksum=original_checksum,
        suffix=".zip",
        path="SKILL.md",
        content=(
            "---\nname: writer\ndescription: Draft stronger prose\n---\n"
            "# Revised instructions"
        ),
    )
    try:
        assert revised.checksum != original_checksum
        assert revised.name == "writer"
        assert revised.description == "Draft stronger prose"
        with zipfile.ZipFile(revised.path) as package:
            assert package.read("assets/logo.bin") == b"unchanged-binary"
            assert b"# Revised instructions" in package.read("SKILL.md")
        source = read_package_markdown(
            package_uri=uri,
            checksum=original_checksum,
            suffix=".zip",
        )
        assert "# Original instructions" in source["content"]
    finally:
        revised.path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_markdown_editor_rejects_unsafe_or_unchanged_edits(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "skill_package_root", str(tmp_path / "packages"))
    payload = archive({
        "SKILL.md": "---\nname: safe\ndescription: Safe package\n---\n# Instructions",
    })
    original = await receive_and_validate(upload(payload))
    checksum = original.checksum
    uri = store_package(
        original.path, "skill-1", "version-1", original.suffix, checksum,
    )
    with pytest.raises(InvalidSkillPackage, match="only Markdown"):
        build_markdown_revision(
            package_uri=uri, checksum=checksum, suffix=".zip",
            path="SKILL.md.exe", content="changed",
        )
    with pytest.raises(InvalidSkillPackage, match="unsafe archive path"):
        build_markdown_revision(
            package_uri=uri, checksum=checksum, suffix=".zip",
            path="../SKILL.md", content="changed",
        )
    with pytest.raises(InvalidSkillPackage, match="unchanged"):
        build_markdown_revision(
            package_uri=uri, checksum=checksum, suffix=".zip",
            path="SKILL.md",
            content="---\nname: safe\ndescription: Safe package\n---\n# Instructions",
        )


def test_s3_store_uses_immutable_versioned_key(tmp_path, monkeypatch):
    class FakeS3:
        def __init__(self):
            self.objects = {}

        def put_object(self, *, Bucket, Key, Body, ContentType, Metadata, IfNoneMatch):
            assert IfNoneMatch == "*"
            if (Bucket, Key) in self.objects:
                error = RuntimeError("precondition failed")
                error.response = {"Error": {"Code": "PreconditionFailed"}}
                raise error
            self.objects[(Bucket, Key)] = {
                "body": Body.read(), "metadata": Metadata,
            }

        def download_file(self, bucket, key, filename):
            Path(filename).write_bytes(self.objects[(bucket, key)]["body"])

        def head_object(self, *, Bucket, Key):
            if (Bucket, Key) not in self.objects:
                raise KeyError(Key)
            value = self.objects[(Bucket, Key)]
            return {
                "Metadata": value["metadata"],
                "ContentLength": len(value["body"]),
            }

        def delete_object(self, *, Bucket, Key):
            self.objects.pop((Bucket, Key), None)

    from pathlib import Path

    monkeypatch.setattr(settings, "skill_s3_bucket", "skills-test")
    source = tmp_path / "skill.zip"
    source.write_bytes(b"immutable")
    checksum = hashlib.sha256(b"immutable").hexdigest()
    client = FakeS3()
    store = S3SkillPackageStore(client=client)
    uri = store.put_file(
        source, skill_id="skill-1", version_id="version-7",
        checksum=checksum, suffix=".zip",
    )
    assert uri == f"s3://skills-test/skills/skill-1/version-7/{checksum}.zip"
    assert store.put_file(
        source, skill_id="skill-1", version_id="version-7",
        checksum=checksum, suffix=".zip",
    ) == uri
    assert store.exists(uri)
    copied = tmp_path / "copied.zip"
    store.copy_to(uri, copied)
    assert copied.read_bytes() == b"immutable"

    source.write_bytes(b"replaced")
    with pytest.raises(SkillStorageError, match="source checksum mismatch"):
        store.put_file(
            source, skill_id="skill-1", version_id="version-7",
            checksum=checksum, suffix=".zip",
        )


def test_cache_preserves_object_store_outage_category(tmp_path):
    class OfflineStore:
        def copy_to(self, uri, destination):
            raise SkillStorageError("object store is offline")

    with pytest.raises(SkillStorageError, match="object store is offline"):
        SkillCacheService(tmp_path / "cache", store=OfflineStore()).ensure(
            skill_id="skill-1", checksum="a" * 64,
            package_uri="s3://skills/skill-1/version-1/package.zip",
        )
