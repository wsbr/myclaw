"""Immutable evaluation definitions, real case Runs, and deterministic scoring."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_eval_v1 import (
    AgentEvalCase, AgentEvalCaseRun, AgentEvalRun, AgentEvalSuite,
    AgentEvalSuiteVersion,
)
from app.models.common import utcnow, uuid7
from app.models.runtime_v1 import Message, Run
from app.models.usage_v1 import UsageLedgerEntry
from app.services.agent_assembly_service import create_draft_test_snapshot
from app.services.chat_service import create_user_session, enqueue_chat_message


def _decimal(value: Any, *, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{field} must be numeric") from error


def normalize_gate_policy(value: dict[str, Any] | None) -> dict[str, Any]:
    raw = dict(value or {})
    unknown = set(raw) - {"min_pass_rate"}
    if unknown:
        raise ValueError(f"unsupported gate policy fields: {', '.join(sorted(unknown))}")
    threshold = _decimal(raw.get("min_pass_rate", "1"), field="min_pass_rate")
    if threshold < 0 or threshold > 1:
        raise ValueError("min_pass_rate must be between 0 and 1")
    return {"min_pass_rate": str(threshold)}


def normalize_assertions(value: dict[str, Any]) -> dict[str, Any]:
    raw = dict(value or {})
    allowed = {
        "contains_all", "contains_none", "min_chars", "max_chars", "max_cost",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"unsupported assertion fields: {', '.join(sorted(unknown))}")
    normalized: dict[str, Any] = {}
    for key in ("contains_all", "contains_none"):
        items = raw.get(key, [])
        if not isinstance(items, list) or any(not isinstance(item, str) or not item for item in items):
            raise ValueError(f"{key} must be a list of non-empty strings")
        if items:
            normalized[key] = items
    for key in ("min_chars", "max_chars"):
        if key in raw and raw[key] is not None:
            number = int(raw[key])
            if number < 0:
                raise ValueError(f"{key} must be nonnegative")
            normalized[key] = number
    if (
        "min_chars" in normalized and "max_chars" in normalized
        and normalized["max_chars"] < normalized["min_chars"]
    ):
        raise ValueError("max_chars must be greater than or equal to min_chars")
    if raw.get("max_cost") is not None:
        cost = raw["max_cost"]
        if not isinstance(cost, dict):
            raise ValueError("max_cost must be an object")
        unknown_cost = set(cost) - {"currency", "amount", "allow_unknown"}
        if unknown_cost:
            raise ValueError(
                f"unsupported max_cost fields: {', '.join(sorted(unknown_cost))}"
            )
        currency = str(cost.get("currency") or "").upper()
        amount = _decimal(cost.get("amount"), field="max_cost.amount")
        if len(currency) != 3 or amount < 0:
            raise ValueError("max_cost requires a 3-letter currency and nonnegative amount")
        normalized["max_cost"] = {
            "currency": currency,
            "amount": str(amount),
            "allow_unknown": bool(cost.get("allow_unknown", False)),
        }
    if not normalized:
        raise ValueError("each eval case requires at least one assertion")
    return normalized


def _definition_hash(
    *, name: str, description: str, gate_policy: dict[str, Any], cases: list[dict[str, Any]],
) -> str:
    payload = {
        "name": name,
        "description": description,
        "gate_policy": gate_policy,
        "cases": cases,
    }
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def normalize_cases(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not 1 <= len(cases) <= 20:
        raise ValueError("an eval suite requires between 1 and 20 cases")
    normalized: list[dict[str, Any]] = []
    keys: set[str] = set()
    for index, raw in enumerate(cases):
        key = str(raw.get("case_key") or "").strip()
        name = str(raw.get("name") or "").strip()
        input_text = str(raw.get("input_text") or "").strip()
        if not key or len(key) > 100 or not name or not input_text:
            raise ValueError("case_key, name, and input_text are required")
        if key in keys:
            raise ValueError(f"duplicate eval case key: {key}")
        keys.add(key)
        weight = _decimal(raw.get("weight", "1"), field=f"cases[{index}].weight")
        if weight <= 0:
            raise ValueError("eval case weight must be positive")
        normalized.append({
            "case_key": key,
            "name": name,
            "input_text": input_text,
            "assertions": normalize_assertions(dict(raw.get("assertions") or {})),
            "weight": str(weight),
        })
    return normalized


async def create_eval_suite(
    db: AsyncSession, *, agent_id: str, actor_account_id: str,
    name: str, description: str, gate_policy: dict[str, Any], cases: list[dict[str, Any]],
) -> tuple[AgentEvalSuite, AgentEvalSuiteVersion]:
    suite = AgentEvalSuite(
        id=uuid7(), agent_id=agent_id, name=name.strip(), status="active",
        next_version_no=1, created_by_account_id=actor_account_id,
    )
    if not suite.name:
        raise ValueError("eval suite name is required")
    db.add(suite)
    await db.flush()
    version = await create_eval_suite_version(
        db, suite=suite, actor_account_id=actor_account_id, name=suite.name,
        description=description, gate_policy=gate_policy, cases=cases,
    )
    return suite, version


async def create_eval_suite_version(
    db: AsyncSession, *, suite: AgentEvalSuite, actor_account_id: str,
    name: str, description: str, gate_policy: dict[str, Any], cases: list[dict[str, Any]],
) -> AgentEvalSuiteVersion:
    normalized_policy = normalize_gate_policy(gate_policy)
    normalized_cases = normalize_cases(cases)
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("eval suite name is required")
    content_hash = _definition_hash(
        name=clean_name, description=description, gate_policy=normalized_policy,
        cases=normalized_cases,
    )
    version = AgentEvalSuiteVersion(
        id=uuid7(), suite_id=suite.id, version_no=suite.next_version_no,
        name=clean_name, description=description, gate_policy=normalized_policy,
        content_hash=content_hash, created_by_account_id=actor_account_id,
        published_at=utcnow(),
    )
    db.add(version)
    await db.flush()
    db.add_all(AgentEvalCase(
        id=uuid7(), suite_version_id=version.id, case_key=item["case_key"],
        name=item["name"], input_text=item["input_text"],
        assertions=item["assertions"], weight=Decimal(item["weight"]), sort_order=index,
    ) for index, item in enumerate(normalized_cases))
    await db.flush()
    suite.name = clean_name
    suite.current_version_id = version.id
    suite.next_version_no += 1
    suite.version += 1
    await db.flush()
    return version


async def start_eval_run(
    db: AsyncSession, *, suite: AgentEvalSuite, expected_draft_revision: int,
    actor_account_id: str, actor_user_id: str, idempotency_record_id: str,
) -> AgentEvalRun:
    if suite.status != "active" or not suite.current_version_id:
        raise ValueError("eval suite is not active")
    version = await db.get(AgentEvalSuiteVersion, suite.current_version_id)
    if version is None:
        raise LookupError("eval suite current version not found")
    cases = list(await db.scalars(select(AgentEvalCase).where(
        AgentEvalCase.suite_version_id == version.id,
    ).order_by(AgentEvalCase.sort_order, AgentEvalCase.id)))
    snapshot = await create_draft_test_snapshot(
        db, agent_id=suite.agent_id, expected_draft_revision=expected_draft_revision,
        actor_account_id=actor_account_id, idempotency_record_id=idempotency_record_id,
    )
    eval_run = AgentEvalRun(
        id=uuid7(), agent_id=suite.agent_id, suite_version_id=version.id,
        candidate_agent_version_id=snapshot.id,
        candidate_content_hash=snapshot.content_hash,
        status="queued", case_count=len(cases), passed_count=0, failed_count=0,
        error_count=0, gate_policy_snapshot=dict(version.gate_policy or {}),
        created_by_account_id=actor_account_id,
    )
    db.add(eval_run)
    await db.flush()
    for case in cases:
        session = await create_user_session(
            db, user_id=actor_user_id, entry_agent_id=suite.agent_id,
            origin_type="web", title=f"Eval · {version.name} · {case.name}",
            allow_unpublished=True, agent_version_id=snapshot.id,
        )
        _, runtime_run, _, _ = await enqueue_chat_message(
            db, session_id=session.id, user_id=actor_user_id,
            content=case.input_text,
            idempotency_record_id=f"eval:{eval_run.id}:{case.id}",
            agent_version_id=snapshot.id, execution_kind="draft_test",
        )
        db.add(AgentEvalCaseRun(
            id=uuid7(), eval_run_id=eval_run.id, eval_case_id=case.id,
            session_id=session.id, run_id=runtime_run.id, status="queued",
            assertion_results=[],
        ))
    await db.flush()
    return eval_run


async def _cost_assertion(
    db: AsyncSession, *, run_id: str, contract: dict[str, Any],
) -> dict[str, Any]:
    currency = contract["currency"]
    amount = Decimal(contract["amount"])
    unknown_count = int(await db.scalar(select(func.count(UsageLedgerEntry.id)).where(
        UsageLedgerEntry.run_id == run_id,
        UsageLedgerEntry.cost_status == "unknown",
    )) or 0)
    unconvertible_count = int(await db.scalar(select(func.count(UsageLedgerEntry.id)).where(
        UsageLedgerEntry.run_id == run_id,
        UsageLedgerEntry.cost_status != "unknown",
        UsageLedgerEntry.currency.is_not(None),
        UsageLedgerEntry.currency != currency,
    )) or 0)
    actual = await db.scalar(select(func.coalesce(func.sum(UsageLedgerEntry.cost_amount), 0)).where(
        UsageLedgerEntry.run_id == run_id,
        UsageLedgerEntry.currency == currency,
    ))
    actual = Decimal(actual or 0)
    allow_unknown = bool(contract.get("allow_unknown", False))
    passed = actual <= amount and (
        allow_unknown or (unknown_count == 0 and unconvertible_count == 0)
    )
    return {
        "assertion": "max_cost", "passed": passed, "currency": currency,
        "maximum": str(amount), "actual": str(actual), "unknown_entry_count": unknown_count,
        "unconvertible_entry_count": unconvertible_count,
    }


async def evaluate_response(
    db: AsyncSession, *, run_id: str, response: str, assertions: dict[str, Any],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for expected in assertions.get("contains_all", []):
        results.append({
            "assertion": "contains", "expected": expected,
            "passed": expected in response,
        })
    for forbidden in assertions.get("contains_none", []):
        results.append({
            "assertion": "not_contains", "expected": forbidden,
            "passed": forbidden not in response,
        })
    if "min_chars" in assertions:
        results.append({
            "assertion": "min_chars", "expected": assertions["min_chars"],
            "actual": len(response), "passed": len(response) >= assertions["min_chars"],
        })
    if "max_chars" in assertions:
        results.append({
            "assertion": "max_chars", "expected": assertions["max_chars"],
            "actual": len(response), "passed": len(response) <= assertions["max_chars"],
        })
    if "max_cost" in assertions:
        results.append(await _cost_assertion(
            db, run_id=run_id, contract=assertions["max_cost"],
        ))
    return results


async def reconcile_one_eval_case(db: AsyncSession) -> bool:
    """Settle one case from its durable runtime Run; safe for repeated polling."""

    case_run = await db.scalar(select(AgentEvalCaseRun).join(
        Run, Run.id == AgentEvalCaseRun.run_id,
    ).where(
        AgentEvalCaseRun.status.in_(("queued", "running")),
        Run.status.in_(("completed", "failed", "cancelled")),
    ).order_by(AgentEvalCaseRun.updated_at, AgentEvalCaseRun.id).with_for_update(skip_locked=True).limit(1))
    if case_run is None:
        return False
    runtime_run = await db.get(Run, case_run.run_id)
    eval_run = await db.get(AgentEvalRun, case_run.eval_run_id)
    case = await db.get(AgentEvalCase, case_run.eval_case_id)
    if runtime_run is None or eval_run is None or case is None:
        case_run.status = "error"
        case_run.failure_code = "eval_binding_missing"
        case_run.score = Decimal(0)
        case_run.ended_at = utcnow()
    elif runtime_run.status != "completed":
        case_run.status = "error"
        case_run.failure_code = f"runtime_{runtime_run.status}"
        case_run.score = Decimal(0)
        case_run.ended_at = utcnow()
    else:
        case_run.started_at = case_run.started_at or runtime_run.created_at
        eval_run.started_at = eval_run.started_at or runtime_run.created_at
        eval_run.status = "running"
        response = await db.scalar(select(Message).where(
            Message.run_id == runtime_run.id,
            Message.sender_type == "agent",
            Message.role == "assistant",
            Message.status == "committed",
        ).order_by(Message.created_at.desc(), Message.id.desc()).limit(1))
        if response is None:
            case_run.status = "error"
            case_run.failure_code = "assistant_response_missing"
            case_run.score = Decimal(0)
            case_run.ended_at = utcnow()
        else:
            results = await evaluate_response(
                db, run_id=runtime_run.id, response=response.content,
                assertions=dict(case.assertions or {}),
            )
            passed = bool(results) and all(item["passed"] for item in results)
            case_run.status = "passed" if passed else "failed"
            case_run.score = Decimal(1) if passed else Decimal(0)
            case_run.response_message_id = response.id
            case_run.assertion_results = results
            case_run.failure_code = None if passed else "assertion_failed"
            case_run.ended_at = utcnow()
    await db.flush()
    await settle_eval_run(db, eval_run_id=case_run.eval_run_id)
    return True


async def settle_eval_run(db: AsyncSession, *, eval_run_id: str) -> AgentEvalRun:
    eval_run = await db.scalar(select(AgentEvalRun).where(
        AgentEvalRun.id == eval_run_id,
    ).with_for_update())
    if eval_run is None:
        raise LookupError("eval run not found")
    rows = list(await db.execute(select(
        AgentEvalCaseRun.status, AgentEvalCaseRun.score, AgentEvalCase.weight,
    ).join(AgentEvalCase, AgentEvalCase.id == AgentEvalCaseRun.eval_case_id).where(
        AgentEvalCaseRun.eval_run_id == eval_run.id,
    )))
    eval_run.passed_count = sum(1 for status, _, _ in rows if status == "passed")
    eval_run.failed_count = sum(1 for status, _, _ in rows if status == "failed")
    eval_run.error_count = sum(1 for status, _, _ in rows if status == "error")
    if any(status in {"queued", "running"} for status, _, _ in rows):
        eval_run.status = "running"
        eval_run.started_at = eval_run.started_at or utcnow()
        return eval_run
    total_weight = sum((Decimal(weight) for _, _, weight in rows), Decimal(0))
    passed_weight = sum((
        Decimal(weight) * Decimal(score or 0) for _, score, weight in rows
    ), Decimal(0))
    score = passed_weight / total_weight if total_weight else Decimal(0)
    threshold = Decimal(str(eval_run.gate_policy_snapshot.get("min_pass_rate", "1")))
    eval_run.score = score
    eval_run.status = "passed" if eval_run.error_count == 0 and score >= threshold else "failed"
    eval_run.ended_at = utcnow()
    eval_run.started_at = eval_run.started_at or eval_run.created_at
    await db.flush()
    return eval_run
