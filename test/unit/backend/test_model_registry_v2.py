"""Immutable ModelVersion and activation/conformance gate tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.model_registry_v2 import _version_view
from app.config import settings
from app.core.platform_secrets import SECRET_REFERENCE_PREFIX, resolve_secret_reference
from app.database import Base
from app.models.common import utcnow
from app.models.registry import ConformanceRun, ModelVersion, PlatformSecret, RegisteredModel
from app.schemas.registry_v1 import ModelVersionCreate
from app.services.model_registry_service import (
    ModelActivationRejected,
    ModelRegistryConflict,
    activate_model_version,
    create_logical_model,
    create_model_version,
)

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_model_version_is_append_only_and_latest_conformance_controls_activation():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.create_all(
            sync, tables=[
                RegisteredModel.__table__, ModelVersion.__table__, ConformanceRun.__table__,
                PlatformSecret.__table__,
            ]
        ))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as db:
        model = await create_logical_model(
            db, key="provider.primary", name="Primary", actor_account_id="admin-1",
        )
        version_values = {
            "provider": "openai_compatible", "model_name": "provider-native",
            "endpoint": "https://provider.test/v1", "api_key": "sk-unit-model-secret",
            "default_parameters": {"temperature": 0},
            "capabilities": {
                "streaming": True, "native_tool_calling": True,
                "tool_result_continuation": True, "stop_reason": True,
            },
        }
        immutable = await create_model_version(
            db, model_id=model.id, expected_version=1, actor_account_id="admin-1",
            values=version_values,
        )
        version_response = await _version_view(db, immutable)
        assert version_response["published_at"] == immutable.published_at.isoformat()
        assert version_response["credential_configured"] is True
        assert version_response["credential_source"] == "platform_secret"
        assert "credential_ref" not in version_response
        assert immutable.credential_ref.startswith(SECRET_REFERENCE_PREFIX)
        assert "sk-unit-model-secret" not in immutable.credential_ref
        assert await resolve_secret_reference(db, immutable.credential_ref) == "sk-unit-model-secret"
        assert model.current_version_id is None
        assert immutable.version_no == 1
        with pytest.raises(ModelRegistryConflict, match="identical immutable"):
            await create_model_version(
                db, model_id=model.id, expected_version=2,
                actor_account_id="admin-1", values=version_values,
            )
        with pytest.raises(ModelActivationRejected):
            await activate_model_version(
                db, model_id=model.id, model_version_id=immutable.id, expected_version=2,
            )

        db.add(ConformanceRun(
            subject_type="model_version", subject_id=immutable.id,
            suite_version="agent-native-v2.1",
            idempotency_key="superseded-suite-pass", request_hash="0" * 64,
            status="passed", started_at=utcnow(), ended_at=utcnow(),
            result_summary={"passed": True}, content_hash="9" * 64,
        ))
        await db.flush()
        with pytest.raises(ModelActivationRejected):
            await activate_model_version(
                db, model_id=model.id, model_version_id=immutable.id, expected_version=2,
            )

        db.add(ConformanceRun(
            subject_type="model_version", subject_id=immutable.id,
            suite_version=settings.provider_conformance_suite_version,
            idempotency_key="pass-old", request_hash="1" * 64,
            status="passed", started_at=utcnow(), ended_at=utcnow(),
            result_summary={"passed": True}, content_hash="2" * 64,
        ))
        await db.flush()
        db.add(ConformanceRun(
            subject_type="model_version", subject_id=immutable.id,
            suite_version=settings.provider_conformance_suite_version,
            idempotency_key="fail-new", request_hash="3" * 64,
            status="failed", started_at=utcnow(), ended_at=utcnow(),
            result_summary={"passed": False}, content_hash="4" * 64,
        ))
        await db.flush()
        with pytest.raises(ModelActivationRejected):
            await activate_model_version(
                db, model_id=model.id, model_version_id=immutable.id, expected_version=2,
            )

        db.add(ConformanceRun(
            subject_type="model_version", subject_id=immutable.id,
            suite_version=settings.provider_conformance_suite_version,
            idempotency_key="pass-latest", request_hash="5" * 64,
            status="passed", started_at=utcnow(), ended_at=utcnow(),
            result_summary={"passed": True}, content_hash="6" * 64,
        ))
        await db.flush()
        activated = await activate_model_version(
            db, model_id=model.id, model_version_id=immutable.id, expected_version=2,
        )
        assert activated.current_version_id == immutable.id
        assert activated.provider == immutable.provider
        assert activated.version == 3
    await engine.dispose()


def test_public_model_version_input_requires_endpoint_and_complete_api_key():
    schema = ModelVersionCreate.model_json_schema()

    assert {"endpoint", "api_key"}.issubset(schema["required"])
    assert "credential_ref" not in schema["properties"]
    assert schema["properties"]["api_key"]["writeOnly"] is True

    with pytest.raises(ValidationError):
        ModelVersionCreate.model_validate({
            "provider": "openai_compatible",
            "model_name": "provider-native",
            "endpoint": "http://provider.test/v1",
            "default_parameters": {},
            "capabilities": {
                "native_tool_calling": True,
                "tool_result_continuation": True,
                "stop_reason": True,
            },
        })
