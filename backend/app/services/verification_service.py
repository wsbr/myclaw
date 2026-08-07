"""Scenario-scoped deterministic verification over authoritative facts only."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import func, select

from app.config import settings
from app.models.artifact_v1 import Artifact
from app.models.common import uuid7
from app.models.foundation import UserSession
from app.models.group_v1 import GroupRun
from app.models.runtime_v1 import Run, ToolAttempt, ToolInvocation, WorkItem
from app.models.verification_v1 import VerificationEvidenceRef, VerificationResult
from app.persistence.reliable_writes import ExecutionEventWriter


CAPACITY_RISK_RULE_KEY = "capacity_risk.v1"
CAPACITY_RISK_RULE_VERSION = "1"
CAPACITY_ARTIFACT_NAMES = (
    "requirements.md",
    "capacity_risk.py",
    "test_report.md",
    "acceptance_report.md",
)


def _json_line(value: dict[str, Any]) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ) + "\n"


@dataclass(frozen=True)
class CapacityCase:
    case_id: str
    argv: tuple[str, ...]
    exit_code: int
    stdout: str


CAPACITY_CASES = (
    CapacityCase("T01", ("python3", "capacity_risk.py", "--capacity", "120", "--committed", "138"), 0, _json_line({
        "committed_hours": 138, "over_capacity_hours": 18, "risk": "high",
        "total_capacity_hours": 120, "utilization_percent": 115.0,
    })),
    CapacityCase("T02", ("python3", "capacity_risk.py", "--capacity", "100", "--committed", "74"), 0, _json_line({
        "committed_hours": 74, "over_capacity_hours": 0, "risk": "low",
        "total_capacity_hours": 100, "utilization_percent": 74.0,
    })),
    CapacityCase("T03", ("python3", "capacity_risk.py", "--capacity", "100", "--committed", "75"), 0, _json_line({
        "committed_hours": 75, "over_capacity_hours": 0, "risk": "medium",
        "total_capacity_hours": 100, "utilization_percent": 75.0,
    })),
    CapacityCase("T04", ("python3", "capacity_risk.py", "--capacity", "100", "--committed", "90"), 0, _json_line({
        "committed_hours": 90, "over_capacity_hours": 0, "risk": "medium",
        "total_capacity_hours": 100, "utilization_percent": 90.0,
    })),
    CapacityCase("T05", ("python3", "capacity_risk.py", "--capacity", "100", "--committed", "91"), 0, _json_line({
        "committed_hours": 91, "over_capacity_hours": 0, "risk": "high",
        "total_capacity_hours": 100, "utilization_percent": 91.0,
    })),
    CapacityCase("T06", ("python3", "capacity_risk.py", "--capacity", "100", "--committed", "0"), 0, _json_line({
        "committed_hours": 0, "over_capacity_hours": 0, "risk": "low",
        "total_capacity_hours": 100, "utilization_percent": 0.0,
    })),
    CapacityCase("T07", ("python3", "capacity_risk.py", "--capacity", "0", "--committed", "10"), 2, _json_line({
        "error": {"code": "invalid_capacity", "message": "total_capacity_hours must be greater than 0"},
    })),
    CapacityCase("T08", ("python3", "capacity_risk.py", "--capacity", "100", "--committed", "-1"), 2, _json_line({
        "error": {"code": "invalid_committed", "message": "committed_hours must be greater than or equal to 0"},
    })),
    CapacityCase("T09", ("python3", "capacity_risk.py", "--capacity", "100"), 2, _json_line({
        "error": {"code": "missing_committed", "message": "--committed is required"},
    })),
)


def verification_evidence_view(row: VerificationEvidenceRef) -> dict[str, Any]:
    ref_id = row.tool_attempt_id or row.artifact_id
    return {
        "evidence_type": row.evidence_type,
        "ref_id": ref_id,
        "external_fact_ref": row.external_fact_ref,
        "workspace_generation": row.workspace_generation,
        "checksum": row.checksum,
    }


async def verification_result_view(db, row: VerificationResult) -> dict[str, Any]:
    refs = list(await db.scalars(select(VerificationEvidenceRef).where(
        VerificationEvidenceRef.verification_result_id == row.id,
    ).order_by(VerificationEvidenceRef.sort_order)))
    return {
        "id": row.id,
        "run_id": row.run_id,
        "rule_key": row.rule_key,
        "rule_version": row.rule_version,
        "verification_epoch": row.verification_epoch,
        "status": row.status,
        "observed_facts": list(row.observed_facts or []),
        "missing_facts": list(row.missing_facts or []),
        "source_refs": [verification_evidence_view(item) for item in refs],
        "created_at": row.created_at.isoformat(),
    }


def _attempt_argv(attempt: ToolAttempt) -> list[str]:
    request = dict(attempt.request_snapshot or {})
    direct = request.get("argv")
    if isinstance(direct, list):
        return [str(item) for item in direct]
    arguments = request.get("arguments")
    if isinstance(arguments, dict) and isinstance(arguments.get("argv"), list):
        return [str(item) for item in arguments["argv"]]
    return []


def _attempt_data(attempt: ToolAttempt) -> dict[str, Any]:
    response = dict(attempt.response_snapshot or {})
    return dict(response.get("data") or {}) if isinstance(response.get("data"), dict) else {}


def _artifact_path(artifact: Artifact) -> Path:
    relative = PurePosixPath(artifact.relative_path)
    if (
        relative.is_absolute() or ".." in relative.parts or len(relative.parts) < 4
        or relative.parts[:2] != (".myclaw", "artifacts")
        or relative.parts[2] != artifact.id
    ):
        raise ValueError("artifact storage identity is invalid")
    root = (Path(settings.sandbox_root) / artifact.session_id).resolve()
    candidate = root.joinpath(*relative.parts).resolve(strict=False)
    candidate.relative_to(root)
    return candidate


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _markdown_checks(name: str, text: str) -> list[tuple[str, bool]]:
    if name == "requirements.md":
        required = (
            "utilization_percent = committed_hours / total_capacity_hours * 100",
            "low: utilization < 75",
            "medium: 75 <= utilization <= 90",
            "high: utilization > 90",
            "over_capacity_hours = max(committed_hours - total_capacity_hours, 0)",
            "total_capacity_hours > 0",
            "committed_hours >= 0",
            *tuple(f"T{index:02d}" for index in range(1, 10)),
        )
    elif name == "test_report.md":
        required = (
            "9/9", "argv", "stdout", "stderr", "exit code",
            *tuple(f"T{index:02d}" for index in range(1, 10)),
        )
    elif name == "acceptance_report.md":
        required = (
            "capacity_risk.v1", "9/9", "accepted",
            *CAPACITY_ARTIFACT_NAMES,
        )
    else:
        return []
    lowered = text.lower()
    return [(token, token.lower() in lowered) for token in required]


def _fact_fingerprint(attempts: list[ToolAttempt], artifacts: list[Artifact]) -> str:
    def storage_fingerprint(item: Artifact) -> str:
        try:
            path = _artifact_path(item)
            return _sha256_bytes(path.read_bytes()) if path.is_file() else "missing"
        except (OSError, ValueError):
            return "unavailable"

    facts = {
        "attempts": [{
            "id": item.id,
            "status": item.status,
            "request": item.request_snapshot,
            "response": item.response_snapshot,
            "request_hash": item.request_hash,
            "outcome_confirmed": item.outcome_confirmed,
        } for item in sorted(attempts, key=lambda row: row.id)],
        "artifacts": [{
            "id": item.id, "name": item.name, "status": item.status,
            "checksum": item.checksum, "size_bytes": item.size_bytes,
            "storage_checksum": storage_fingerprint(item),
        } for item in sorted(artifacts, key=lambda row: row.id)],
    }
    return hashlib.sha256(json.dumps(
        facts, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode()).hexdigest()


async def capacity_risk_facts_exist(db, run_id: str) -> bool:
    artifact = await db.scalar(select(Artifact.id).join(
        WorkItem, WorkItem.id == Artifact.work_item_id,
    ).where(
        WorkItem.run_id == run_id,
        Artifact.name.in_(CAPACITY_ARTIFACT_NAMES),
    ).limit(1))
    if artifact is not None:
        return True
    attempts = list(await db.scalars(select(ToolAttempt).join(
        ToolInvocation, ToolInvocation.id == ToolAttempt.tool_invocation_id,
    ).where(
        ToolInvocation.run_id == run_id,
        ToolInvocation.tool_key == "execute",
    )))
    return any("capacity_risk.py" in _attempt_argv(item) for item in attempts)


async def verify_capacity_risk(db, *, run_id: str, actor_id: str = "verification") -> VerificationResult:
    """Evaluate capacity_risk.v1 without calling a model, Tool, or evaluator."""

    run = await db.scalar(select(Run).where(Run.id == run_id).with_for_update())
    if run is None:
        raise LookupError(f"run not found: {run_id}")
    await db.scalar(select(UserSession).where(
        UserSession.id == run.session_id,
    ).execution_options(populate_existing=True).with_for_update())
    attempts = list(await db.scalars(select(ToolAttempt).join(
        ToolInvocation, ToolInvocation.id == ToolAttempt.tool_invocation_id,
    ).where(
        ToolInvocation.run_id == run_id,
        ToolInvocation.tool_key == "execute",
    ).order_by(ToolAttempt.created_at, ToolAttempt.id)))
    relevant_attempts = [
        item for item in attempts if "capacity_risk.py" in _attempt_argv(item)
    ]
    artifacts = list(await db.scalars(select(Artifact).join(
        WorkItem, WorkItem.id == Artifact.work_item_id,
    ).where(
        WorkItem.run_id == run_id,
        Artifact.name.in_(CAPACITY_ARTIFACT_NAMES),
    ).order_by(Artifact.created_at, Artifact.id)))
    fingerprint = _fact_fingerprint(relevant_attempts, artifacts)
    replay = await db.scalar(select(VerificationResult).where(
        VerificationResult.run_id == run_id,
        VerificationResult.rule_key == CAPACITY_RISK_RULE_KEY,
        VerificationResult.rule_version == CAPACITY_RISK_RULE_VERSION,
        VerificationResult.fact_fingerprint == fingerprint,
    ))
    if replay is not None:
        return replay

    observed: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    evidence: list[tuple[str, str, str | None]] = []
    expected_by_argv = {case.argv: case for case in CAPACITY_CASES}
    actual_by_argv: dict[tuple[str, ...], list[ToolAttempt]] = {}
    for attempt in relevant_attempts:
        argv = tuple(_attempt_argv(attempt))
        actual_by_argv.setdefault(argv, []).append(attempt)
        if argv not in expected_by_argv:
            failed.append({
                "fact": "unexpected_capacity_attempt", "tool_attempt_id": attempt.id,
                "argv": list(argv),
            })
    script_checksums: set[str] = set()
    for case in CAPACITY_CASES:
        matches = actual_by_argv.get(case.argv, [])
        if not matches:
            missing.append({"fact": "tool_attempt", "case_id": case.case_id, "argv": list(case.argv)})
            continue
        if len(matches) != 1:
            failed.append({
                "fact": "duplicate_tool_attempt", "case_id": case.case_id,
                "tool_attempt_ids": [item.id for item in matches],
            })
            continue
        attempt = matches[0]
        data = _attempt_data(attempt)
        canonical_request_hash = hashlib.sha256(json.dumps(
            dict(attempt.request_snapshot or {}), ensure_ascii=False,
            sort_keys=True, separators=(",", ":"), default=str,
        ).encode()).hexdigest()
        checks = {
            "attempt_status": attempt.status == "succeeded",
            "attempt_identity": bool(attempt.id and attempt.tool_invocation_id),
            "request_hash_exact": attempt.request_hash == canonical_request_hash,
            "execution_timing_recorded": bool(
                attempt.started_at and attempt.ended_at
                and attempt.ended_at >= attempt.started_at
            ),
            "execution_fence_recorded": (
                attempt.execution_epoch >= 1 and attempt.fencing_token >= 1
            ),
            "argv_exact": _attempt_argv(attempt) == list(case.argv),
            "exit_code_exact": data.get("exit_code") == case.exit_code,
            "stdout_exact": data.get("stdout") == case.stdout,
            "stderr_exact": data.get("stderr") == "",
            "not_timed_out": data.get("timed_out") is False,
            "stdout_complete": data.get("stdout_truncated") is False,
            "stderr_complete": data.get("stderr_truncated") is False,
            "outcome_confirmed": attempt.outcome_confirmed is True,
        }
        input_checksums = data.get("input_file_checksums")
        script_checksum = (
            input_checksums.get("capacity_risk.py")
            if isinstance(input_checksums, dict) else None
        )
        checks["tested_file_checksum_recorded"] = bool(script_checksum)
        if script_checksum:
            script_checksums.add(str(script_checksum))
        fact = {
            "case_id": case.case_id,
            "tool_attempt_id": attempt.id,
            "argv": _attempt_argv(attempt),
            "exit_code": data.get("exit_code"),
            "stdout": data.get("stdout"),
            "stderr": data.get("stderr"),
            "script_checksum": script_checksum,
            "checks": checks,
        }
        observed.append(fact)
        evidence.append(("tool_attempt", attempt.id, None))
        if not all(checks.values()):
            failed.append({
                "fact": "case_mismatch", "case_id": case.case_id,
                "tool_attempt_id": attempt.id,
                "failed_checks": [key for key, value in checks.items() if not value],
            })

    if len(relevant_attempts) < 9:
        missing.append({
            "fact": "attempt_cardinality", "expected": 9,
            "observed": len(relevant_attempts),
        })
    elif len(relevant_attempts) > 9:
        failed.append({
            "fact": "attempt_cardinality", "expected": 9,
            "observed": len(relevant_attempts),
        })
    if len(script_checksums) > 1:
        failed.append({
            "fact": "tested_script_changed_between_cases",
            "checksums": sorted(script_checksums),
        })

    by_name: dict[str, list[Artifact]] = {}
    for artifact in artifacts:
        by_name.setdefault(artifact.name, []).append(artifact)
    capacity_artifact_checksum = None
    for name in CAPACITY_ARTIFACT_NAMES:
        matches = by_name.get(name, [])
        if not matches:
            missing.append({"fact": "artifact", "name": name})
            continue
        if len(matches) != 1:
            failed.append({
                "fact": "duplicate_artifact", "name": name,
                "artifact_ids": [item.id for item in matches],
            })
            continue
        artifact = matches[0]
        checks: dict[str, bool] = {"published": artifact.status == "published"}
        content = b""
        try:
            path = _artifact_path(artifact)
            content = path.read_bytes()
            checks["downloadable"] = path.is_file()
            checks["size_exact"] = len(content) == artifact.size_bytes
            checks["checksum_exact"] = _sha256_bytes(content) == artifact.checksum
        except (OSError, ValueError):
            checks.update({"downloadable": False, "size_exact": False, "checksum_exact": False})
        content_checks: list[tuple[str, bool]] = []
        if name.endswith(".md"):
            try:
                content_checks = _markdown_checks(name, content.decode("utf-8"))
            except UnicodeDecodeError:
                content_checks = [("utf8", False)]
            checks["content_contract"] = bool(content_checks) and all(
                passed for _, passed in content_checks
            )
        elif name == "capacity_risk.py":
            capacity_artifact_checksum = artifact.checksum
            checks["tested_bytes"] = (
                len(script_checksums) == 1 and artifact.checksum in script_checksums
            )
            checks["nonempty_program"] = bool(content)
        observed.append({
            "artifact_id": artifact.id, "name": name,
            "checksum": artifact.checksum, "size_bytes": artifact.size_bytes,
            "checks": checks,
            "content_checks": [
                {"token": token, "present": present}
                for token, present in content_checks
            ],
        })
        evidence.append(("artifact", artifact.id, artifact.checksum))
        if not all(checks.values()):
            failed.append({
                "fact": "artifact_mismatch", "name": name,
                "artifact_id": artifact.id,
                "failed_checks": [key for key, value in checks.items() if not value],
            })
    if len(artifacts) < 4:
        missing.append({
            "fact": "artifact_cardinality", "expected": 4,
            "observed": len(artifacts),
        })
    elif len(artifacts) > 4:
        failed.append({
            "fact": "artifact_cardinality", "expected": 4,
            "observed": len(artifacts),
        })
    if capacity_artifact_checksum and script_checksums and capacity_artifact_checksum not in script_checksums:
        failed.append({
            "fact": "published_program_not_tested",
            "artifact_checksum": capacity_artifact_checksum,
            "tested_checksums": sorted(script_checksums),
        })

    status = "failed" if failed else "insufficient_evidence" if missing else "passed"
    all_missing = [*missing, *failed]
    epoch = int(await db.scalar(select(func.max(VerificationResult.verification_epoch)).where(
        VerificationResult.run_id == run_id,
        VerificationResult.rule_key == CAPACITY_RISK_RULE_KEY,
        VerificationResult.rule_version == CAPACITY_RISK_RULE_VERSION,
    )) or 0) + 1
    group_run_id = await db.scalar(select(GroupRun.id).where(GroupRun.run_id == run_id))
    result = VerificationResult(
        id=uuid7(), run_id=run_id, group_run_id=group_run_id,
        rule_key=CAPACITY_RISK_RULE_KEY,
        rule_version=CAPACITY_RISK_RULE_VERSION,
        verification_epoch=epoch, status=status,
        fact_fingerprint=fingerprint, observed_facts=observed,
        missing_facts=all_missing,
    )
    db.add(result)
    await db.flush()
    for index, (kind, ref_id, checksum) in enumerate(evidence, start=1):
        db.add(VerificationEvidenceRef(
            verification_result_id=result.id, sort_order=index,
            evidence_type=kind,
            tool_attempt_id=ref_id if kind == "tool_attempt" else None,
            artifact_id=ref_id if kind == "artifact" else None,
            external_fact_ref=None, workspace_generation=None,
            checksum=checksum,
            evidence_key=f"{kind}:{ref_id}",
        ))
    await db.flush()
    await ExecutionEventWriter(db).append(
        session_id=run.session_id, run_id=run.id,
        group_run_id=group_run_id,
        event_type="verification.completed",
        event_dedupe_key=f"verification:{result.id}:completed",
        actor_type="service", actor_id=actor_id,
        entity_type="verification_result", entity_id=result.id,
        entity_version=epoch, visibility="user",
        summary=f"{CAPACITY_RISK_RULE_KEY} verification {status}",
        payload={
            "verification_result_id": result.id,
            "rule_key": CAPACITY_RISK_RULE_KEY,
            "status": status,
            "passed_cases": sum(
                1 for item in observed
                if item.get("case_id") and all(item.get("checks", {}).values())
            ),
            "artifact_count": sum(1 for item in observed if item.get("artifact_id")),
        },
        correlation_id=run.id,
    )
    return result


async def maybe_verify_capacity_risk(
    db, *, run_id: str, actor_id: str = "verification",
) -> VerificationResult | None:
    if not await capacity_risk_facts_exist(db, run_id):
        return None
    return await verify_capacity_risk(db, run_id=run_id, actor_id=actor_id)
