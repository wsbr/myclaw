"""Exact Skill snapshots and progressive runtime disclosure."""

import io
import zipfile
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.datastructures import Headers, UploadFile

from app.config import settings
from app.database import Base
from app.models.agent_v1 import AgentAssemblyVersion, AgentVersionSkill
from app.models.registry import ModelVersion, SkillVersion
from app.models.runtime_v1 import DurableRunner, RunnerSkillSnapshot
from app.core.tool_registry.base import RuntimeContext
from app.core.tool_registry.builtin.read_file import ReadFileTool
from app.core.tool_registry.builtin.execute import _resolve_frozen_skill_argv
from app.runtime.snapshot_resolver import SnapshotResolver
from app.services.skill_catalog_service import (
    SkillCatalogOverflow, build_skill_catalog_sections,
)
from app.services.skill_package_service import receive_and_validate, store_package


pytestmark = pytest.mark.unit


def version(**overrides):
    values = {
        "id": "sv1",
        "skill_id": "skill1",
        "checksum": "a" * 64,
        "manifest_name": "evidence-writer",
        "manifest_description": "Read evidence and produce a concise report.",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_skill_catalog_contains_only_bounded_metadata_and_read_path():
    sections = build_skill_catalog_sections([version()], max_tokens=160)
    assert len(sections) == 1
    content = sections[0]["content"]
    assert "evidence-writer" in content
    assert f"skill://skill1/{'a' * 64}/SKILL.md" in content
    assert "Read evidence" in content
    assert "version_id" not in content
    assert "sv1" not in content
    assert "scripts/" not in content


def test_skill_catalog_fails_instead_of_silently_omitting_bound_skills():
    items = [version(id=f"sv{index}", skill_id=f"s{index}") for index in range(20)]
    with pytest.raises(SkillCatalogOverflow):
        build_skill_catalog_sections(items, max_tokens=64)


@pytest.mark.asyncio
async def test_execute_resolves_only_explicit_frozen_skill_uri(tmp_path, monkeypatch):
    root = tmp_path / "cache" / "skill1" / ("a" * 64)
    script = root / "scripts" / "check.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('ok')", encoding="utf-8")
    uri = f"skill://skill1/{'a' * 64}/scripts/check.py"

    async def resolved(db, *, context, value):
        assert value == uri
        return script, value

    monkeypatch.setattr(settings, "shared_skill_root", str(tmp_path / "cache"))
    monkeypatch.setattr(
        "app.core.tool_registry.builtin.execute.resolve_read_path", resolved,
    )
    context = RuntimeContext(
        session_id="session1", runner_id="runner1", user_id="user1", agent_id="agent1",
    )
    argv, readonly_roots, inputs = await _resolve_frozen_skill_argv(
        object(), context, ["python", uri, "output.json"],
    )
    assert argv == ["python", str(script), "output.json"]
    assert readonly_roots == {str(root.resolve())}
    assert inputs == {uri: script}


@pytest.mark.asyncio
async def test_snapshot_resolver_persists_exact_runner_skill_snapshot(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def no_policies(*args, **kwargs):
        return {}

    async def no_cards(*args, **kwargs):
        return []

    monkeypatch.setattr(
        "app.runtime.snapshot_resolver.resolve_frozen_runtime_policies", no_policies,
    )
    monkeypatch.setattr("app.runtime.snapshot_resolver.discover_agent_cards", no_cards)

    now = datetime.utcnow()
    async with factory() as db:
        db.add_all([
            ModelVersion(
                id="mv1", model_id="model1", version_no=1,
                provider="openai", model_name="test", default_parameters={},
                capabilities={}, adapter_protocol="agent_native.v2",
                content_hash="m" * 64, published_at=now,
            ),
            AgentAssemblyVersion(
                id="av1", agent_id="agent1", version_no=1,
                publication_status="published", name="Agent", description="",
                instructions="", model_id="model1", model_version_id="mv1",
                group_enabled=False, discovery_config={}, collaboration_config={},
                created_by_account_id="account1", immutable_at=now,
            ),
            SkillVersion(
                id="sv1", skill_id="skill1", version_no=1,
                status="published", package_uri="skill-package://skill1/sv1/package.zip",
                checksum="a" * 64, manifest={}, content_hash="h" * 64,
                manifest_name="Writer", manifest_description="Write clearly",
                created_by_account_id="account1", published_at=now,
            ),
            AgentVersionSkill(
                agent_version_id="av1", skill_id="skill1",
                skill_version_id="sv1", checksum="a" * 64, sort_order=0,
            ),
            DurableRunner(
                id="runner1", work_item_id="work1", run_id="run1",
                session_id="session1", agent_id="agent1", agent_version_id="av1",
                execution_epoch=1, status="running", principal_snapshot={},
                assembly_snapshot={}, prompt_manifest={}, input_snapshot={},
            ),
        ])
        await db.commit()
        runner = await db.get(DurableRunner, "runner1")
        resolved = await SnapshotResolver().resolve(
            db, runner=runner, execution_principal={"account_id": "account1"},
            permission_mode="direct",
        )
        await db.commit()

        snapshot = await db.scalar(select(RunnerSkillSnapshot).where(
            RunnerSkillSnapshot.runner_id == "runner1",
            RunnerSkillSnapshot.skill_id == "skill1",
        ))
        assert [item.id for item in resolved.skill_versions] == ["sv1"]
        assert snapshot.skill_version_id == "sv1"
        assert snapshot.checksum == "a" * 64
        assert snapshot.path_uri == f"skill://skill1/{'a' * 64}/"

    await engine.dispose()


@pytest.mark.asyncio
async def test_read_file_materializes_only_the_frozen_skill_version(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "skill_storage_backend", "local")
    monkeypatch.setattr(settings, "skill_package_root", str(tmp_path / "packages"))
    monkeypatch.setattr(settings, "shared_skill_root", str(tmp_path / "cache"))
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr(
            "SKILL.md",
            "---\nname: Frozen Writer\ndescription: Exact instructions\n---\nUse v1 only.",
        )
        archive.writestr("references/rules.md", "Evidence first.")
    upload = UploadFile(
        filename="writer.zip", file=io.BytesIO(payload.getvalue()), headers=Headers(),
    )
    validated = await receive_and_validate(upload)
    uri = store_package(
        validated.path, "skill1", "sv1", validated.suffix, validated.checksum,
    )

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.utcnow()
    async with factory() as db:
        db.add_all([
            SkillVersion(
                id="sv1", skill_id="skill1", version_no=1,
                status="deprecated", package_uri=uri,
                checksum=validated.checksum, manifest=validated.manifest,
                content_hash="h" * 64, manifest_name=validated.name,
                manifest_description=validated.description,
                created_by_account_id="account1", published_at=now,
            ),
            RunnerSkillSnapshot(
                runner_id="runner1", skill_id="skill1", skill_version_id="sv1",
                checksum=validated.checksum,
                path_uri=f"skill://skill1/{validated.checksum}/", sort_order=0,
            ),
        ])
        await db.commit()
        context = RuntimeContext(
            session_id="session1", runner_id="runner1", user_id="user1",
            agent_id="agent1",
        )
        result = await ReadFileTool().execute({
            "path": f"skill://skill1/{validated.checksum}/SKILL.md",
        }, context, db=db)
        assert result.success
        assert "Use v1 only" in result.data["content"]
        assert result.data["checksum"]

        denied_context = context.model_copy(update={"runner_id": "runner2"})
        with pytest.raises(ValueError, match="not in this Runner"):
            await ReadFileTool().execute({
                "path": f"skill://skill1/{validated.checksum}/SKILL.md",
            }, denied_context, db=db)
    await engine.dispose()
