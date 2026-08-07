#!/usr/bin/env python3
"""Collect immutable, credential-free phase-one UAT measurements from MySQL."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
from decimal import Decimal
from urllib.parse import urlsplit

import pymysql


def scalar(cursor, statement: str):
    cursor.execute(statement)
    row = cursor.fetchone()
    return next(iter(row.values())) if row else None


def rows(cursor, statement: str) -> list[dict]:
    cursor.execute(statement)
    return list(cursor.fetchall())


def ratio(numerator: int, denominator: int) -> dict:
    return {
        "passed": int(numerator),
        "total": int(denominator),
        "rate": round(numerator / denominator, 6) if denominator else None,
    }


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: collect_uat_evidence.py /absolute/path/to/evidence.json")
    target = Path(sys.argv[1]).resolve()
    connection = pymysql.connect(
        host=os.environ.get("MYCLAW_GOAL1_MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ["MYCLAW_GOAL1_MYSQL_PORT"]),
        user=os.environ["MYCLAW_GOAL1_MYSQL_USER"],
        password=os.environ["MYCLAW_GOAL1_MYSQL_PASSWORD"],
        database=os.environ["MYCLAW_GOAL1_MYSQL_DATABASE"],
        cursorclass=pymysql.cursors.DictCursor,
    )
    with connection:
        with connection.cursor() as cursor:
            attempts = rows(cursor, """
                SELECT pa.id,pa.agent_turn_id,pa.attempt_no,pa.provider,pa.status,
                       pa.received_payload,pa.tool_call_observed,pa.error_code,
                       pa.started_at,pa.first_payload_at,pa.ended_at,pa.usage
                  FROM provider_attempt pa
                 ORDER BY pa.created_at,pa.id
            """)
            turns = rows(cursor, """
                SELECT id,run_id,status,stop_reason,schema_repair_count,
                       started_at,ended_at,`usage`
                  FROM agent_turn ORDER BY created_at,id
            """)
            run_rows = rows(cursor, """
                SELECT id,mode,status,terminal_reason,created_at,completed_at
                  FROM run ORDER BY created_at,id
            """)
            first = [item for item in attempts if int(item["attempt_no"]) == 1]
            final_attempts: dict[str, dict] = {}
            for item in attempts:
                final_attempts[item["agent_turn_id"]] = item
            token_input = 0
            token_output = 0
            for item in attempts:
                usage = item["usage"]
                if isinstance(usage, str):
                    usage = json.loads(usage)
                usage = usage or {}
                token_input += int(usage.get("input_tokens") or 0)
                token_output += int(usage.get("output_tokens") or 0)
            pricing_limit = int(os.environ["MYCLAW_PRICING_TIER_MAX_INPUT_TOKENS"])
            if any(
                int((json.loads(item["usage"]) if isinstance(item["usage"], str) else
                     (item["usage"] or {})).get("input_tokens") or 0) > pricing_limit
                for item in attempts
            ):
                raise RuntimeError("a provider attempt exceeded the frozen pricing tier")
            first_payload_ms = [
                int((item["first_payload_at"] - item["started_at"]).total_seconds() * 1000)
                for item in attempts
                if item["first_payload_at"] is not None and item["started_at"] is not None
            ]
            turn_ms = [
                int((item["ended_at"] - item["started_at"]).total_seconds() * 1000)
                for item in turns
                if item["ended_at"] is not None and item["started_at"] is not None
            ]
            run_ms = [
                int((item["completed_at"] - item["created_at"]).total_seconds() * 1000)
                for item in run_rows
                if item["completed_at"] is not None and item["created_at"] is not None
            ]
            tool_status = rows(cursor, """
                SELECT status,COUNT(*) AS count FROM tool_invocation GROUP BY status ORDER BY status
            """)
            agent_status = rows(cursor, """
                SELECT status,COUNT(*) AS count FROM agent_invocation GROUP BY status ORDER BY status
            """)
            verification_status = rows(cursor, """
                SELECT status,COUNT(*) AS count FROM verification_result GROUP BY status ORDER BY status
            """)
            verification_total = sum(int(item["count"]) for item in verification_status)
            verification_passed = sum(
                int(item["count"]) for item in verification_status if item["status"] == "passed"
            )
            artifacts = rows(cursor, """
                SELECT id,name,checksum,size_bytes,mime_type,status,work_item_id,created_at
                  FROM artifact ORDER BY created_at,id
            """)
            # PyMySQL's cursor helper above intentionally accepts only fixed SQL;
            # this parameterized query is kept explicit so names cannot alter it.
            cursor.execute("""
                SELECT a.id,a.current_published_version_id,av.version_no,av.content_hash,
                       av.name,mv.id AS model_version_id,mv.model_name,mv.provider,
                       mv.default_parameters AS model_default_parameters,
                       mv.capabilities AS model_capabilities,
                       mv.content_hash AS model_content_hash
                  FROM agent a
                  JOIN agent_version av ON av.id=a.current_published_version_id
                  JOIN model_version mv ON mv.id=av.model_version_id
                 WHERE av.name LIKE %s
                 ORDER BY av.name,a.id
            """, (f"%{os.environ['MYCLAW_ACCEPTANCE_COMMIT_SHA'][:8]}",))
            agents = list(cursor.fetchall())
            schema = {
                "alembic_head": scalar(cursor, "SELECT version_num FROM alembic_version"),
                "legacy_table_count": int(scalar(cursor, """
                    SELECT COUNT(*) FROM information_schema.TABLES
                     WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN
                     ('plan_execution','plan_work_item_node','plan_work_item_dependency',
                      'plan_declared_agent','group_formation_proposal','group_formation_revision',
                      'group_formation_participant','group_formation_work_item',
                      'group_formation_suggested_edge','group_agent_node',
                      'group_initial_suggested_edge','group_run_agent_version',
                      'group_collaboration_edge')
                """)),
                "legacy_run_column_count": int(scalar(cursor, """
                    SELECT COUNT(*) FROM information_schema.COLUMNS
                     WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='run'
                       AND COLUMN_NAME IN
                       ('lifecycle','outcome','execution_kind','entry_agent_id',
                        'source_run_id','result_summary','error_code','started_at','finished_at')
                """)),
                "archive_seal_trigger_count": int(scalar(cursor, """
                    SELECT COUNT(*) FROM information_schema.TRIGGERS
                     WHERE TRIGGER_SCHEMA=DATABASE()
                       AND EVENT_OBJECT_TABLE='legacy_uat_fact_archive'
                """)),
            }
            incidents = {
                "provider_outcome_unknown": int(scalar(cursor,
                    "SELECT COUNT(*) FROM provider_attempt WHERE status='outcome_unknown'")),
                "tool_outcome_unknown": int(scalar(cursor,
                    "SELECT COUNT(*) FROM tool_invocation WHERE status='outcome_unknown'")),
                "agent_rejected": int(scalar(cursor,
                    "SELECT COUNT(*) FROM agent_invocation WHERE status='rejected'")),
                "membership_invitation_rejected": int(scalar(cursor, """
                    SELECT COUNT(*) FROM execution_event
                     WHERE event_type='group.membership_invitation_rejected'
                """)),
                "permission_denied": int(scalar(cursor, """
                    SELECT COUNT(*) FROM tool_invocation
                     WHERE error_code='permission_mode_denied'
                """)),
                "failed_runs": int(scalar(cursor,
                    "SELECT COUNT(*) FROM run WHERE status='failed'")),
                "cancelled_runs": int(scalar(cursor,
                    "SELECT COUNT(*) FROM run WHERE status='cancelled'")),
                "late_results": int(scalar(cursor,
                    "SELECT COUNT(*) FROM late_result_observation")),
                "duplicate_side_effect_events": int(scalar(cursor, """
                    SELECT COUNT(*) FROM execution_event
                     WHERE event_type LIKE '%duplicate%' OR summary LIKE '%duplicate%'
                """)),
                "lost_runners": int(scalar(cursor,
                    "SELECT COUNT(*) FROM runner_v1 WHERE status='lost'")),
            }
            wait_metrics = {
                "approval_wait_ms_average": scalar(cursor, """
                    SELECT ROUND(AVG(TIMESTAMPDIFF(MICROSECOND,created_at,decided_at))/1000,3)
                      FROM approval WHERE decided_at IS NOT NULL
                """),
                "clarification_wait_ms_average": scalar(cursor, """
                    SELECT ROUND(AVG(TIMESTAMPDIFF(MICROSECOND,created_at,updated_at))/1000,3)
                      FROM clarification WHERE status='answered'
                """),
                "waiting_run_count": int(scalar(cursor, """
                    SELECT COUNT(*) FROM run
                     WHERE status IN ('waiting_user','waiting_approval','waiting_tool','waiting_agent')
                """)),
            }

    project_root = Path(__file__).resolve().parents[2]
    openapi = project_root / "docs/technical/openapi-v1.yaml"
    endpoint = urlsplit(os.environ["MYCLAW_REAL_LLM_ENDPOINT"])
    input_price = Decimal(os.environ["MYCLAW_INPUT_PRICE_PER_MILLION"])
    output_price = Decimal(os.environ["MYCLAW_OUTPUT_PRICE_PER_MILLION"])
    estimated_cost = (
        Decimal(token_input) * input_price + Decimal(token_output) * output_price
    ) / Decimal(1_000_000)
    payload = {
        "schema": "myclaw.phase_one_uat_evidence.v1",
        "commit_sha": os.environ["MYCLAW_ACCEPTANCE_COMMIT_SHA"],
        "release": os.environ["MYCLAW_RELEASE"],
        "environment": "uat_single_node",
        "provider": {
            "model": os.environ["MYCLAW_REAL_LLM_MODEL"],
            "endpoint_origin": f"{endpoint.scheme}://{endpoint.netloc}",
            "credential_ref": "env://MYCLAW_REAL_LLM_API_KEY",
        },
        "agents": agents,
        "schema_contract": schema,
        "metrics": {
            "provider_first_attempt_success": ratio(
                sum(item["status"] == "succeeded" for item in first), len(first)
            ),
            "provider_final_success": ratio(
                sum(item["status"] == "succeeded" for item in final_attempts.values()),
                len(final_attempts),
            ),
            "provider_retry_count": sum(int(item["attempt_no"]) > 1 for item in attempts),
            "provider_repair_count": 0,
            "first_payload_ms": {
                "samples": len(first_payload_ms),
                "average": round(sum(first_payload_ms) / len(first_payload_ms), 3)
                if first_payload_ms else None,
                "maximum": max(first_payload_ms) if first_payload_ms else None,
            },
            "turn_duration_ms": {
                "samples": len(turn_ms),
                "average": round(sum(turn_ms) / len(turn_ms), 3) if turn_ms else None,
                "maximum": max(turn_ms) if turn_ms else None,
            },
            "run_active_ms": {
                "samples": len(run_ms),
                "average": round(sum(run_ms) / len(run_ms), 3) if run_ms else None,
                "maximum": max(run_ms) if run_ms else None,
            },
            "tokens": {
                "input": token_input, "output": token_output,
                "total": token_input + token_output,
            },
            "cost": {
                "estimated_amount": format(estimated_cost.quantize(Decimal("0.000001")), "f"),
                "currency": os.environ["MYCLAW_COST_CURRENCY"],
                "input_price_per_million_tokens": format(input_price, "f"),
                "output_price_per_million_tokens": format(output_price, "f"),
                "max_input_tokens_per_attempt": pricing_limit,
                "pricing_effective_date": os.environ["MYCLAW_PRICING_EFFECTIVE_DATE"],
                "pricing_source": os.environ["MYCLAW_PRICING_SOURCE_URL"],
                "method": "observed provider token usage multiplied by the frozen public list-price tier",
            },
            "tool_invocations": tool_status,
            "agent_invocations": agent_status,
            "plan_update_count": 0,
            "waiting": wait_metrics,
            "verified_pass_rate": ratio(verification_passed, verification_total),
            "incidents": incidents,
        },
        "runs": run_rows,
        "artifacts": artifacts,
        "trace_counts": {
            "runner_traces": 0,
            "spans": 0,
        },
        "trace_index": {"runners": [], "spans": [], "links": []},
        "openapi_sha256": hashlib.sha256(openapi.read_bytes()).hexdigest(),
    }
    # Queries whose values are needed after the connection context are resolved
    # in one short read-only reconnect, keeping the JSON assembly straightforward.
    with pymysql.connect(
        host=os.environ.get("MYCLAW_GOAL1_MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ["MYCLAW_GOAL1_MYSQL_PORT"]),
        user=os.environ["MYCLAW_GOAL1_MYSQL_USER"],
        password=os.environ["MYCLAW_GOAL1_MYSQL_PASSWORD"],
        database=os.environ["MYCLAW_GOAL1_MYSQL_DATABASE"],
        cursorclass=pymysql.cursors.DictCursor,
    ) as connection2:
        with connection2.cursor() as cursor:
            payload["metrics"]["provider_repair_count"] = int(scalar(cursor,
                "SELECT COALESCE(SUM(schema_repair_count),0) FROM agent_turn"))
            payload["metrics"]["plan_update_count"] = int(scalar(cursor,
                "SELECT COUNT(*) FROM plan_update"))
            payload["trace_counts"]["runner_traces"] = int(scalar(cursor,
                "SELECT COUNT(*) FROM runner_trace"))
            payload["trace_counts"]["spans"] = int(scalar(cursor,
                "SELECT COUNT(*) FROM observability_span"))
            payload["trace_index"]["runners"] = rows(cursor, """
                SELECT id,trace_id,root_span_id,runner_id,run_id,work_item_id,
                       agent_id,status,sampled,error_code,started_at,ended_at
                  FROM runner_trace ORDER BY created_at,id
            """)
            payload["trace_index"]["spans"] = rows(cursor, """
                SELECT id,runner_trace_id,span_id,parent_span_id,name,span_kind,
                       status,started_at,ended_at,duration_ms,input_tokens,
                       output_tokens,error_code
                  FROM observability_span ORDER BY started_at,id
            """)
            payload["trace_index"]["links"] = rows(cursor, """
                SELECT id,span_id,linked_trace_id,linked_span_id,link_type,
                       entity_type,entity_id
                  FROM observability_span_link ORDER BY created_at,id
            """)
    if len(agents) != 5:
        raise RuntimeError(f"expected exactly five SHA-bound UAT Agents, found {len(agents)}")
    if schema != {
        "alembic_head": "0029",
        "legacy_table_count": 0,
        "legacy_run_column_count": 0,
        "archive_seal_trigger_count": 3,
    }:
        raise RuntimeError(f"schema contract mismatch: {schema}")
    if verification_total < 10 or verification_passed != verification_total:
        raise RuntimeError(
            f"verified pass-rate gate failed: {verification_passed}/{verification_total}"
        )
    if not attempts or len(first_payload_ms) != sum(
        item["status"] == "succeeded" for item in attempts
    ):
        raise RuntimeError("first-payload timing is incomplete for succeeded attempts")
    if token_input + token_output <= 0:
        raise RuntimeError("real Provider returned no token usage")
    if not payload["trace_counts"]["runner_traces"] or not payload["trace_counts"]["spans"]:
        raise RuntimeError("Runner trace evidence is missing")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
