from __future__ import annotations

import pytest

from app.core.tool_registry.base import RuntimeContext, ToolResult
from app.runtime.action_dispatcher import ActionDispatcher, _prepare_tool_call
from app.runtime.protocol import ToolCallItem


pytestmark = pytest.mark.unit


def _definition(*, normalization: bool) -> dict:
    return {
        "name": "typed_schedule_probe",
        "input_schema": {
            "type": "object",
            "properties": {
                "max_occurrences": {"type": ["integer", "null"], "minimum": 1},
                "trigger": {
                    "type": "object",
                    "properties": {
                        "interval_seconds": {"type": "integer", "minimum": 60},
                    },
                    "required": ["interval_seconds"],
                    "additionalProperties": False,
                },
            },
            "required": ["max_occurrences", "trigger"],
            "additionalProperties": False,
        },
        "output_schema": {},
        "side_effect": "none",
        "confirmation_policy": "never",
        "execution_mode": "inline",
        "annotations": {"lossless_argument_normalization": normalization},
    }


def _call(*, maximum: str = "10") -> ToolCallItem:
    return ToolCallItem(
        call_id="typed-call", name="typed_schedule_probe",
        arguments={
            "max_occurrences": maximum,
            "trigger": {"interval_seconds": "60"},
        },
        index=0,
    )


def _context() -> RuntimeContext:
    return RuntimeContext(
        tenant_id="tenant-1", session_id="session-1", runner_id="runner-1",
        user_id="user-1", agent_id="agent-1",
    )


@pytest.mark.asyncio
async def test_opted_in_tool_executes_with_lossless_integer_normalization():
    observed = []

    async def execute(_name, arguments, _context, _db):
        observed.append(arguments)
        return ToolResult(status="succeeded", summary="normalized")

    definition = _definition(normalization=True)
    original = _call()
    dispatcher = ActionDispatcher([definition], executor=execute)
    result = (await dispatcher.dispatch_batch(
        [original],
        capability_snapshot={"effective_tool_keys": ["typed_schedule_probe"]},
        runtime_context=_context(),
    ))[0]

    assert result.result.success
    assert observed == [{
        "max_occurrences": 10,
        "trigger": {"interval_seconds": 60},
    }]
    # The provider-authored Turn Item is immutable evidence.
    assert original.arguments["max_occurrences"] == "10"
    prepared, repairs = _prepare_tool_call(original, definition)
    assert prepared.arguments["max_occurrences"] == 10
    assert repairs == [
        {
            "path": "/max_occurrences", "from_type": "string",
            "to_type": "integer", "rule": "canonical_integer_string",
        },
        {
            "path": "/trigger/interval_seconds", "from_type": "string",
            "to_type": "integer", "rule": "canonical_integer_string",
        },
    ]


@pytest.mark.asyncio
async def test_frozen_schedule_tool_version_receives_runtime_compatibility_fix():
    observed = []

    async def execute(_name, arguments, _context, _db):
        observed.append(arguments)
        return ToolResult(status="succeeded", summary="normalized")

    definition = _definition(normalization=False)
    definition["name"] = "schedule_task"
    call = _call()
    call = ToolCallItem(
        call_id=call.call_id, name="schedule_task",
        arguments=call.arguments, index=call.index,
    )
    dispatcher = ActionDispatcher([definition], executor=execute)
    result = (await dispatcher.dispatch_batch(
        [call],
        capability_snapshot={"effective_tool_keys": ["schedule_task"]},
        runtime_context=_context(),
    ))[0]

    assert result.result.success
    assert observed[0]["max_occurrences"] == 10
    assert observed[0]["trigger"]["interval_seconds"] == 60


@pytest.mark.asyncio
@pytest.mark.parametrize(("normalization", "maximum"), [
    (False, "10"),
    (True, "010"),
])
async def test_non_opted_or_noncanonical_values_remain_schema_errors(
    normalization, maximum,
):
    dispatcher = ActionDispatcher([_definition(normalization=normalization)])
    result = (await dispatcher.dispatch_batch(
        [_call(maximum=maximum)],
        capability_snapshot={"effective_tool_keys": ["typed_schedule_probe"]},
        runtime_context=_context(),
    ))[0]

    assert result.schema_error is True
    assert result.result.error.code == "tool_arguments_schema_error"
    assert "Do not repeat the unchanged arguments" in result.result.error.message
    assert result.result.error.details["issues"][0]["validator"] == "type"
