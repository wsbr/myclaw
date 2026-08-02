#!/usr/bin/env python3
"""Create a sanitized, reproducible phase-one evidence manifest from JUnit files."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree


def _junit_summary(path: Path) -> dict:
    root = ElementTree.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    totals = {name: sum(int(suite.attrib.get(name, 0)) for suite in suites)
              for name in ("tests", "failures", "errors", "skipped")}
    totals["duration_seconds"] = round(
        sum(float(suite.attrib.get("time", 0)) for suite in suites), 3
    )
    properties: dict[str, str] = {}
    for prop in root.findall(".//property"):
        name = prop.attrib.get("name")
        if name and (name.startswith("scene_") or name in {"commit_sha", "model", "chromium_ui_chat_plan_group_admin"}):
            properties[name] = prop.attrib.get("value", "")
    totals["properties"] = properties
    totals["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    totals["path"] = str(path)
    totals["passed"] = totals["failures"] == 0 and totals["errors"] == 0
    return totals


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--unit-report", type=Path, required=True)
    parser.add_argument("--mock-report", type=Path, required=True)
    parser.add_argument("--real-report", type=Path, required=True)
    parser.add_argument("--coverage-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--ci-run-url", default="")
    parser.add_argument("--frontend-build", choices=("passed", "failed"), required=True)
    parser.add_argument("--empty-migration", choices=("passed", "failed"), required=True)
    args = parser.parse_args()

    commit_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
    ).stdout.strip()
    dirty = bool(subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        check=True, capture_output=True, text=True,
    ).stdout.strip())
    coverage_root = ElementTree.parse(args.coverage_report).getroot()
    reports = {
        "unit": _junit_summary(args.unit_report),
        "mock_fullstack": _junit_summary(args.mock_report),
        "real_model_fullstack": _junit_summary(args.real_report),
    }
    manifest = {
        "schema": "myclaw.phase_one.acceptance_evidence.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "commit_sha": commit_sha,
        "tracked_worktree_dirty": dirty,
        "model": args.model,
        "endpoint": args.endpoint,
        "credential": "environment/secret injection only; value intentionally not collected",
        "ci_run_url": args.ci_run_url or None,
        "commands": {
            "unit": "test/scripts/run_unit.sh --junitxml=test-results/unit.xml --cov-branch ...",
            "frontend": "pnpm --dir frontend install --frozen-lockfile && pnpm --dir frontend build && docker build ./frontend",
            "migration": "alembic upgrade head on an empty MySQL 8 schema; alembic heads must equal one",
            "mock": "MYCLAW_RUNTIME_WORKER_REPLICAS=2 test/scripts/run_fullstack.sh --junitxml=test-results/mock-fullstack.xml",
            "real": "MYCLAW_LLM_MODE=real with Endpoint/Model and API Key injected by environment; test/scripts/run_fullstack.sh --junitxml=test-results/real-model-fullstack.xml",
        },
        "gates": {
            "frontend_build": args.frontend_build,
            "empty_migration": args.empty_migration,
            "reports": reports,
            "critical_branch_coverage_percent": float(coverage_root.attrib["branch-rate"]) * 100,
            "coverage_sha256": hashlib.sha256(args.coverage_report.read_bytes()).hexdigest(),
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
