#!/usr/bin/env python3
"""Collect and verify sanitized current-SHA Goal 1 acceptance evidence."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import json
import os
import subprocess
from xml.etree import ElementTree

import pymysql


ROOT = Path(__file__).resolve().parents[2]


def _run(*args: str) -> str:
    return subprocess.run(
        args, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _junit(path: Path) -> dict:
    root = ElementTree.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    totals = {
        key: sum(int(suite.attrib.get(key, 0)) for suite in suites)
        for key in ("tests", "failures", "errors", "skipped")
    }
    totals["duration_seconds"] = round(
        sum(float(suite.attrib.get("time", 0)) for suite in suites), 3
    )
    totals["properties"] = {
        prop.attrib["name"]: prop.attrib.get("value", "")
        for prop in root.findall(".//property") if prop.attrib.get("name")
    }
    totals["passed"] = totals["failures"] == 0 and totals["errors"] == 0
    return totals


def _container_image(project: str, compose_file: Path, service: str) -> dict:
    container_ids = _run(
        "docker", "compose", "-p", project, "-f", str(compose_file),
        "ps", "-q", service,
    ).splitlines()
    if not container_ids:
        raise RuntimeError(f"No running container for {project}/{service}")
    container_id = container_ids[0]
    container = json.loads(_run("docker", "inspect", container_id))[0]
    image = json.loads(_run("docker", "image", "inspect", container["Image"]))[0]
    return {
        "project": project,
        "service": service,
        "replica_count": len(container_ids),
        "container_id": container["Id"],
        "container_created": container["Created"],
        "container_status": container["State"]["Status"],
        "health": (container["State"].get("Health") or {}).get("Status"),
        "image_id": image["Id"],
        "repo_digests": image.get("RepoDigests") or [],
        "revision": (image.get("Config", {}).get("Labels") or {}).get(
            "org.opencontainers.image.revision"
        ),
    }


def _conformance_db_row(run_id: str) -> dict:
    connection = pymysql.connect(
        host="127.0.0.1",
        port=int(os.getenv("UAT_MYSQL_PORT", "3310")),
        user=os.environ["MYSQL_USER"],
        password=os.environ["MYSQL_PASSWORD"],
        database=os.environ["MYSQL_DATABASE"],
        cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id,subject_type,subject_id,suite_version,status,request_hash,"
                "content_hash,started_at,ended_at,result_summary FROM conformance_run "
                "WHERE id=%s",
                (run_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError(f"ConformanceRun {run_id} is not present in UAT MySQL")
            if isinstance(row["result_summary"], str):
                row["result_summary"] = json.loads(row["result_summary"])
            return row
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--mock-project", required=True)
    args = parser.parse_args()

    root = args.evidence_root
    required_files = {
        "provider_conformance": root / "provider-conformance.json",
        "mock_fullstack": root / "mock-fullstack.xml",
        "fullstack_smoke": root / "fullstack-smoke.json",
        "schema_head": root / "schema-head.txt",
        "logs": root / "logs-sanitized.txt",
    }
    missing = [str(path) for path in required_files.values() if not path.is_file()]
    if missing:
        raise SystemExit(f"Missing evidence files: {', '.join(missing)}")

    actual_sha = _run("git", "-C", str(ROOT), "rev-parse", "HEAD")
    dirty = bool(_run("git", "-C", str(ROOT), "status", "--porcelain"))
    if actual_sha != args.commit_sha:
        raise SystemExit(f"HEAD {actual_sha} does not match evidence SHA {args.commit_sha}")

    test_compose = ROOT / "test/docker-compose.test.yml"
    uat_compose = ROOT / "deploy/docker-compose.uat.yml"
    images = {
        "mock": [
            _container_image(args.mock_project, test_compose, service)
            for service in ("mock-llm", "api-test", "runtime-worker-test", "sandbox-worker-test")
        ],
        "uat": [
            _container_image("myclaw-uat", uat_compose, service)
            for service in ("api", "runtime-worker")
        ],
    }
    image_path = root / "images.json"
    image_path.write_text(
        json.dumps(images, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )

    provider = _json(required_files["provider_conformance"])
    smoke = _json(required_files["fullstack_smoke"])
    junit = _junit(required_files["mock_fullstack"])
    conformance = provider.get("conformance_run") or {}
    db_evidence = _conformance_db_row(conformance.get("id", ""))
    provider["database_evidence"] = db_evidence
    required_files["provider_conformance"].write_text(
        json.dumps(provider, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )

    coordination = next(
        (
            probe for probe in (conformance.get("result_summary") or {}).get("probes", [])
            if probe.get("name") == "runtime_coordination_tool_schemas"
        ),
        {},
    )
    image_revisions = [
        item["revision"] for group in images.values() for item in group
    ]
    gates = {
        "clean_worktree": not dirty,
        "head_matches": actual_sha == args.commit_sha,
        "suite_version": conformance.get("suite_version") == "agent-native-v2.2",
        "real_provider_passed": provider.get("passed") is True
        and conformance.get("status") == "passed"
        and db_evidence.get("status") == "passed",
        "coordination_schema_calls": coordination.get("passed") is True
        and [case.get("tool_name") for case in coordination.get("details", {}).get("cases", [])]
        == ["update_plan", "ask_user", "invoke_agent"]
        and all(
            case.get("passed") is True
            for case in coordination.get("details", {}).get("cases", [])
        ),
        "mock_fullstack_passed": junit["passed"] and junit["tests"] == 1,
        "mock_smoke_bound": smoke.get("commit_sha") == args.commit_sha,
        "ordinary_final_message": (
            smoke.get("execution", {}).get("final_message")
            == "Agent-native v2 full-stack smoke passed."
        ),
        "schema_head_0024": "0024" in required_files["schema_head"].read_text(encoding="utf-8"),
        "all_application_images_bound": bool(image_revisions)
        and all(revision == args.commit_sha for revision in image_revisions),
    }
    reports = {name: {
        "path": str(path.relative_to(ROOT)), "sha256": _sha256(path),
    } for name, path in required_files.items()}
    reports["images"] = {
        "path": str(image_path.relative_to(ROOT)), "sha256": _sha256(image_path),
    }
    manifest = {
        "schema": "myclaw.agent_native_v2.goal1_evidence.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "commit_sha": args.commit_sha,
        "git_tree": _run("git", "-C", str(ROOT), "rev-parse", "HEAD^{tree}"),
        "tracked_worktree_dirty": dirty,
        "suite_version": "agent-native-v2.2",
        "target_provider": {
            "provider": provider["target"]["provider"],
            "endpoint": provider["target"]["endpoint"],
            "model": provider["target"]["model"],
            "model_version_id": provider["model_version"]["id"],
            "model_version_content_hash": provider["model_version"]["content_hash"],
            "conformance_run_id": conformance["id"],
            "conformance_content_hash": db_evidence["content_hash"],
        },
        "mock_execution": {
            "agent_version_id": smoke["agent"]["agent_version_id"],
            "agent_version_content_hash": smoke["agent"]["agent_version_content_hash"],
            "run_id": smoke["execution"]["run_id"],
        },
        "images": images,
        "junit": junit,
        "gates": gates,
        "reports": reports,
        "passed": all(gates.values()),
    }
    manifest_path = root / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    print(manifest_path)
    return 0 if manifest["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
