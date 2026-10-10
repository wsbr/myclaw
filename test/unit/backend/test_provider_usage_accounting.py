"""Provider reports survive normalization, failure recording and ledger writes."""

import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.model_hub.anthropic import AnthropicLLM
from app.core.model_hub.openai import OpenAILLM
from app.database import Base
from app.models import *  # noqa: F401,F403
from app.models.common import utcnow
from app.models.foundation import UserIdentity, UserSession
from app.models.registry import ModelVersion
from app.models.runtime_v1 import AgentTurn, DurableRunner, ProviderAttempt, Run, WorkItem
from app.models.usage_v1 import UsageLedgerEntry
from app.runtime.model_adapter import ModelAdapter
from app.runtime.protocol import ProviderRequest, ProviderSchemaError
from app.runtime.provider_attempt_recorder import TurnRecorder


pytestmark = pytest.mark.unit
REQUEST = ProviderRequest(messages=({"role": "user", "content": "test"},), tools=(), parameters={})
IDENTITY = SimpleNamespace(
    runner_id="runner", work_item_id="work", run_id="run",
    agent_version_id="agent-version", model_version_id="model-version", provider="openai",
)


@pytest_asyncio.fixture
async def accounting_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as db:
        db.add_all([
            UserIdentity(user_id="user", status="active"),
            UserSession(id="session", user_id="user", account_id="account", tenant_id="tenant",
                        entry_agent_id="agent", status="active", origin_type="web", permission_mode="direct"),
            Run(id="run", session_id="session", user_id="user", account_id="account", tenant_id="tenant",
                mode="chat", status="active", hard_budget_snapshot={}),
            WorkItem(id="work", run_id="run", session_id="session", user_id="user", tenant_id="tenant",
                     mode="chat", owner_agent_id="agent", agent_version_id="agent-version",
                     goal="test", status="running", execution_epoch=1),
            DurableRunner(id="runner", work_item_id="work", run_id="run", session_id="session",
                          tenant_id="tenant", agent_id="agent", agent_version_id="agent-version",
                          execution_epoch=1, status="running", principal_snapshot={},
                          assembly_snapshot={}, prompt_manifest={}, input_snapshot={}),
            ModelVersion(id="model-version", model_id="model", version_no=1, provider="openai",
                         model_name="test-model", default_parameters={}, capabilities={"usage_in_stream": True},
                         adapter_protocol="agent_native.v2", content_hash="a" * 64, published_at=utcnow()),
            AgentTurn(id="turn", runner_id="runner", work_item_id="work", run_id="run",
                      agent_version_id="agent-version", capability_snapshot_id="capability", turn_no=1,
                      status="started", context_manifest=[], input_hash="b" * 64, turn_items=[], usage={}),
        ])
    try:
        async with factory() as db:
            yield db
    finally:
        await engine.dispose()


def openai_provider(completions):
    model = OpenAILLM.__new__(OpenAILLM)
    model.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    model.tool_strict = False
    return model


def wire_usage(cache):
    return SimpleNamespace(prompt_tokens=100, completion_tokens=20,
                           prompt_tokens_details=SimpleNamespace(cached_tokens=cache))


async def recorder(db):
    return TurnRecorder(db, await db.get(AgentTurn, "turn"), IDENTITY, usage_ledger_enabled=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("cache", [None, 0, 37])
async def test_openai_cache_report_reaches_attempt_and_ledger(accounting_db, cache):
    class Completions:
        async def create(self, **kwargs):
            async def chunks():
                yield SimpleNamespace(id="response-id", usage=wire_usage(cache), choices=[SimpleNamespace(
                    finish_reason="stop", delta=SimpleNamespace(content="ok", tool_calls=None),
                )])
            return chunks()

    observed = await recorder(accounting_db)
    result = await ModelAdapter(openai_provider(Completions())).invoke(REQUEST, attempt_observer=observed.observe)
    attempt = await accounting_db.scalar(select(ProviderAttempt))
    entries = {row.metric: row for row in await accounting_db.scalars(select(UsageLedgerEntry))}
    assert result.usage.cached_input_tokens == cache
    assert attempt.usage == result.usage.as_dict()
    assert ("cached_input_tokens" in attempt.usage) == (cache is not None)
    assert entries["input_tokens"].quantity == 100
    assert entries["input_tokens"].measurement_status == "measured"
    assert entries["cached_input_tokens"].quantity == (cache or 0)
    assert entries["cached_input_tokens"].measurement_status == ("unknown" if cache is None else "measured")
    assert len(entries) == 5


@pytest.mark.asyncio
async def test_usage_capability_does_not_turn_missing_response_usage_into_measured_zero(accounting_db):
    class Completions:
        async def create(self, **kwargs):
            async def chunks():
                yield SimpleNamespace(usage=None, choices=[SimpleNamespace(
                    finish_reason="stop", delta=SimpleNamespace(content="ok", tool_calls=None),
                )])
            return chunks()

    observed = await recorder(accounting_db)
    result = await ModelAdapter(openai_provider(Completions())).invoke(REQUEST, attempt_observer=observed.observe)
    assert result.usage.as_dict() == {}
    entries = {row.metric: row for row in await accounting_db.scalars(select(UsageLedgerEntry))}
    for metric in ("input_tokens", "output_tokens", "cached_input_tokens"):
        assert entries[metric].quantity == 0
        assert entries[metric].measurement_status == "unknown"
    assert entries["requests"].measurement_status == "measured"
    assert entries["active_milliseconds"].measurement_status == "measured"


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TimeoutError(), type("SDKTimeout", (Exception,), {"code": None})("Request timed out.")])
async def test_timeout_is_classified_before_durable_recording_and_safe_retry(accounting_db, error):
    class Completions:
        calls = 0
        async def create(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise error
            async def chunks():
                yield SimpleNamespace(usage=wire_usage(0), choices=[SimpleNamespace(
                    finish_reason="stop", delta=SimpleNamespace(content="recovered", tool_calls=None),
                )])
            return chunks()

    completions = Completions()
    observed = await recorder(accounting_db)
    await ModelAdapter(openai_provider(completions)).invoke(REQUEST, attempt_observer=observed.observe)
    attempts = list(await accounting_db.scalars(select(ProviderAttempt).order_by(ProviderAttempt.attempt_no)))
    assert completions.calls == 2
    assert [row.status for row in attempts] == ["failed", "succeeded"]
    assert attempts[0].error_code == "provider_timeout"
    assert not attempts[0].received_payload
    first_tokens = list(await accounting_db.scalars(select(UsageLedgerEntry).where(
        UsageLedgerEntry.provider_attempt_id == attempts[0].id, UsageLedgerEntry.unit == "token",
    )))
    assert all(row.measurement_status == "unknown" for row in first_tokens)


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", ['{"secret":"DO_NOT_PERSIST", "value":', '["DO_NOT_PERSIST"]'])
async def test_schema_failure_retains_safe_usage_and_finish_metadata_without_replay(accounting_db, arguments):
    class Completions:
        calls = 0
        async def create(self, **kwargs):
            self.calls += 1
            async def chunks():
                yield SimpleNamespace(id="schema-response-id", model="test-model", usage=None, choices=[SimpleNamespace(
                    finish_reason=None, delta=SimpleNamespace(content=None, tool_calls=[SimpleNamespace(
                        index=0, id="tool-id", function=SimpleNamespace(name="write_file", arguments=arguments),
                    )]),
                )])
                yield SimpleNamespace(id="schema-response-id", usage=wire_usage(37), choices=[SimpleNamespace(
                    finish_reason="length", delta=SimpleNamespace(content=None, tool_calls=None),
                )])
            return chunks()

    completions = Completions()
    observed = await recorder(accounting_db)
    with pytest.raises(ProviderSchemaError) as raised:
        await ModelAdapter(openai_provider(completions)).invoke(REQUEST, attempt_observer=observed.observe)
    assert completions.calls == 1
    assert raised.value.received_payload
    attempt = await accounting_db.scalar(select(ProviderAttempt))
    assert attempt.status == "outcome_unknown"
    assert attempt.error_code == "provider_schema_error"
    assert attempt.stop_reason == "max_tokens"
    assert attempt.provider_request_id == "schema-response-id"
    assert attempt.usage == {"input_tokens": 100, "output_tokens": 20, "cached_input_tokens": 37}
    assert not attempt.tool_call_observed and not attempt.side_effect_committed
    detail = json.loads(attempt.error_detail)
    assert "DO_NOT_PERSIST" not in attempt.error_detail
    assert detail["provider_stop_reason"] == "length"
    assert detail["diagnostics"]["argument_bytes"] == len(arguments.encode("utf-8"))
    if arguments.startswith("{"):
        assert detail["diagnostics"]["json_error_position"] == len(arguments)
    entries = list(await accounting_db.scalars(select(UsageLedgerEntry).where(UsageLedgerEntry.unit == "token")))
    assert all(row.measurement_status == "measured" for row in entries)


@pytest.mark.asyncio
@pytest.mark.parametrize("cache", [None, 0, 37])
async def test_chat_and_anthropic_stream_preserve_cache_presence(cache):
    class Completions:
        async def create(self, **kwargs):
            return SimpleNamespace(usage=wire_usage(cache), model="test-model", choices=[SimpleNamespace(
                message=SimpleNamespace(content="ok", tool_calls=[]), finish_reason="stop",
            )])
    response = await openai_provider(Completions()).chat([], None, {})
    assert response.usage.cached_input_tokens == cache

    final = SimpleNamespace(usage=SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=cache),
                            content=[], stop_reason="end_turn", model="test-model", id="anthropic-response-id")
    class MessageStream:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        def __aiter__(self): return self
        async def __anext__(self): raise StopAsyncIteration
        async def get_final_message(self): return final
    class Messages:
        def stream(self, **kwargs): return MessageStream()
        async def create(self, **kwargs): return final
    model = AnthropicLLM.__new__(AnthropicLLM)
    model.client = SimpleNamespace(messages=Messages())
    model.tool_strict = True
    result = await ModelAdapter(model).invoke(REQUEST)
    assert result.usage.cached_input_tokens == cache
    response = await model.chat([], None, {})
    assert response.usage.cached_input_tokens == cache
