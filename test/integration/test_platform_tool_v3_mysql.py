"""Real MySQL 8 gates for immutable provider-neutral Tool contracts."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.common import uuid7
from app.models.registry import (
    PlatformSecret,
    ToolConnection,
    ToolConnectionRevision,
    ToolDefinition,
    ToolGovernance,
    ToolVersion,
)


pytestmark = pytest.mark.integration


def _database_url() -> str:
    value = os.getenv("MYCLAW_V3_ACCEPTANCE_DATABASE_URL")
    if not value:
        pytest.skip("MYCLAW_V3_ACCEPTANCE_DATABASE_URL selects the real migrated MySQL database")
    return value


@pytest.mark.asyncio
async def test_mysql_tool_contract_schema_and_immutability_triggers():
    engine = create_async_engine(_database_url(), isolation_level="READ COMMITTED")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid7().replace("-", "")[:12]
    secret_id = uuid7()
    connection_id = uuid7()
    revision_id = uuid7()
    definition_id = uuid7()
    version_id = uuid7()
    primary_connection_id = uuid7()

    async with factory.begin() as db:
        tables = set((await db.execute(text(
            "SELECT TABLE_NAME FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN "
            "('platform_secret','platform_tool','tool_version','tool_connection',"
            "'tool_connection_revision','tool_governance','browser_session_state')"
        ))).scalars())
        assert tables == {
            "platform_secret", "platform_tool", "tool_version", "tool_connection",
            "tool_connection_revision", "tool_governance", "browser_session_state",
        }
        legacy_columns = set((await db.execute(text(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='platform_tool' AND COLUMN_NAME IN "
            "('input_schema','output_schema','implementation_ref','execution_mode',"
            "'enabled','confirmation_policy','platform_config','registration_hash')"
        ))).scalars())
        assert legacy_columns == set()
        exact_columns = set((await db.execute(text(
            "SELECT CONCAT(TABLE_NAME,'.',COLUMN_NAME) FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND ("
            "(TABLE_NAME='tool_invocation' AND COLUMN_NAME IN "
            "('tool_version_id','tool_version_content_hash','connection_revision_id','secret_fingerprint')) OR "
            "(TABLE_NAME='runner_tool_snapshot' AND COLUMN_NAME IN "
            "('tool_version_id','tool_version_content_hash','connection_revision_id','secret_fingerprint')) OR "
            "(TABLE_NAME='agent_turn' AND COLUMN_NAME='presented_schema_bundle_hash') OR "
            "(TABLE_NAME='runner_checkpoint' AND COLUMN_NAME IN "
            "('loaded_tool_version_ids','presented_schema_bundle_hash')))"
        ))).scalars())
        assert exact_columns == {
            "tool_invocation.tool_version_id",
            "tool_invocation.tool_version_content_hash",
            "tool_invocation.connection_revision_id",
            "tool_invocation.secret_fingerprint",
            "runner_tool_snapshot.tool_version_id",
            "runner_tool_snapshot.tool_version_content_hash",
            "runner_tool_snapshot.connection_revision_id",
            "runner_tool_snapshot.secret_fingerprint",
            "agent_turn.presented_schema_bundle_hash",
            "runner_checkpoint.loaded_tool_version_ids",
            "runner_checkpoint.presented_schema_bundle_hash",
        }
        primary_slot = await db.scalar(text(
            "SELECT COUNT(*) FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='tool_connection' "
            "AND COLUMN_NAME='primary_search_slot' AND EXTRA LIKE '%STORED GENERATED%'"
        ))
        assert primary_slot == 1
        non_nullable = set((await db.execute(text(
            "SELECT CONCAT(TABLE_NAME,'.',COLUMN_NAME) FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND IS_NULLABLE='NO' AND ("
            "(TABLE_NAME='tool_invocation' AND COLUMN_NAME IN ('tool_version_id','tool_version_content_hash')) OR "
            "(TABLE_NAME='runner_tool_snapshot' AND COLUMN_NAME IN ('tool_version_id','tool_version_content_hash')))"
        ))).scalars())
        assert non_nullable == {
            "tool_invocation.tool_version_id",
            "tool_invocation.tool_version_content_hash",
            "runner_tool_snapshot.tool_version_id",
            "runner_tool_snapshot.tool_version_content_hash",
        }
        triggers = set((await db.execute(text(
            "SELECT TRIGGER_NAME FROM information_schema.TRIGGERS "
            "WHERE TRIGGER_SCHEMA=DATABASE() AND TRIGGER_NAME LIKE 'trg_%'"
        ))).scalars())
        assert {
            "trg_tool_version_immutable_update",
            "trg_tool_version_immutable_delete",
            "trg_tool_connection_revision_immutable_update",
            "trg_tool_connection_revision_immutable_delete",
            "trg_platform_secret_material_immutable",
            "trg_agent_version_tool_snapshot_required",
            "trg_agent_version_tool_tool_binding_immutable",
            "trg_tool_invocation_tool_binding_immutable",
            "trg_runner_tool_snapshot_tool_binding_immutable",
        } <= triggers

        db.add(PlatformSecret(
            id=secret_id,
            purpose=f"integration:{suffix}",
            ciphertext="aesgcm:v1:database-never-decrypts-test-material",
            fingerprint="a" * 64,
            status="active",
        ))
        connection = ToolConnection(
            id=connection_id,
            connection_key=f"integration.{suffix}",
            display_name="Integration MCP",
            connection_type="mcp",
            auth_type="bearer",
            status="active",
            is_primary=False,
            next_revision_no=2,
            health_status="untested",
        )
        db.add(connection)
        await db.flush()
        db.add(ToolConnectionRevision(
            id=revision_id,
            connection_id=connection_id,
            revision_no=1,
            endpoint="https://example.com/mcp",
            auth_type="bearer",
            secret_id=secret_id,
            secret_fingerprint="a" * 64,
            config={},
            content_hash="b" * 64,
        ))
        await db.flush()
        connection.current_revision_id = revision_id
        definition = ToolDefinition(
            id=definition_id,
            tool_key=f"integration_{suffix}",
            namespace="integration",
            origin="mcp",
            display_name="Integration Tool",
            display_name_zh="集成工具",
            summary="immutable integration contract",
            status="active",
            next_version_no=2,
        )
        db.add(definition)
        await db.flush()
        db.add(ToolVersion(
            id=version_id,
            definition_id=definition_id,
            version_no=1,
            model_description="WHEN: test. WHEN NOT: mutate. RETURNS: data. RECOVERY: stop.",
            input_schema={"type": "object", "additionalProperties": False},
            output_schema={"type": "object"},
            examples=[],
            error_catalog=[],
            annotations={},
            implementation_ref=f"mcp://{connection_id}/integration",
            implementation_version="1",
            execution_mode="durable_async",
            side_effect="non_idempotent",
            supports_cancel=False,
            supports_status_lookup=False,
            default_connection_id=connection_id,
            content_hash="c" * 64,
        ))
        await db.flush()
        definition.current_version_id = version_id
        db.add(ToolGovernance(
            tool_definition_id=definition_id,
            enabled=False,
            confirmation_policy="per_call",
            network_policy={"connection_id": connection_id},
            workspace_policy={},
            risk_rules=[{"when": "unknown_side_effect", "confirmation": "per_call"}],
        ))

    async with factory.begin() as db:
        db.add(ToolConnection(
            id=primary_connection_id,
            connection_key=f"primary.{suffix}",
            display_name="Primary Search",
            connection_type="brave", auth_type="x_api_key",
            status="active", is_primary=True,
            next_revision_no=1, health_status="untested",
        ))

    async with factory() as db:
        db.add(ToolConnection(
            id=uuid7(), connection_key=f"second-primary.{suffix}",
            display_name="Second Primary Search",
            connection_type="bocha", auth_type="bearer",
            status="active", is_primary=True,
            next_revision_no=1, health_status="untested",
        ))
        with pytest.raises(DBAPIError):
            await db.flush()
        await db.rollback()

    async with factory() as db:
        with pytest.raises(DBAPIError, match="tool_version is immutable"):
            await db.execute(update(ToolVersion).where(
                ToolVersion.id == version_id,
            ).values(model_description="mutated"))
        await db.rollback()

    async with factory() as db:
        with pytest.raises(DBAPIError, match="tool_connection_revision is immutable"):
            await db.execute(update(ToolConnectionRevision).where(
                ToolConnectionRevision.id == revision_id,
            ).values(endpoint="https://example.org/mcp"))
        await db.rollback()

    async with factory.begin() as db:
        await db.execute(update(PlatformSecret).where(
            PlatformSecret.id == secret_id,
        ).values(status="rotated", version=2))
        assert await db.scalar(select(PlatformSecret.status).where(
            PlatformSecret.id == secret_id,
        )) == "rotated"

    async with factory() as db:
        with pytest.raises(DBAPIError, match="platform secret material is immutable"):
            await db.execute(update(PlatformSecret).where(
                PlatformSecret.id == secret_id,
            ).values(ciphertext="aesgcm:v1:tampered"))
        await db.rollback()

    await engine.dispose()
