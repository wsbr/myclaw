#!/usr/bin/env python3
"""Build a sanitized platform Tool v3 release-evidence manifest."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
from xml.etree import ElementTree


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _junit(path: Path) -> dict:
    root = ElementTree.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    result = {
        key: sum(int(suite.attrib.get(key, 0)) for suite in suites)
        for key in ("tests", "failures", "errors", "skipped")
    }
    result["duration_seconds"] = round(
        sum(float(suite.attrib.get("time", 0)) for suite in suites), 3
    )
    result["properties"] = {
        item.attrib["name"]: item.attrib.get("value", "")
        for item in root.findall(".//property")
        if item.attrib.get("name", "").startswith(("real_", "blind_", "multistep_", "plan_", "group_"))
    }
    result.update({
        "path": str(path), "sha256": _digest(path),
        "passed": result["failures"] == 0 and result["errors"] == 0,
    })
    return result


def _files(root: Path) -> list[dict]:
    if not root.is_dir():
        return []
    return [
        {"path": str(path), "size": path.stat().st_size, "sha256": _digest(path)}
        for path in sorted(root.rglob("*")) if path.is_file()
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contracts-report", type=Path, required=True)
    parser.add_argument("--mysql-report", type=Path, required=True)
    parser.add_argument("--real-report", type=Path, required=True)
    parser.add_argument("--schema-contract", type=Path, required=True)
    parser.add_argument("--database-facts", type=Path, required=True)
    parser.add_argument("--service-list", type=Path, required=True)
    parser.add_argument("--browser-evidence-dir", type=Path, required=True)
    parser.add_argument("--artifact-checksums", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    commit_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
    ).stdout.strip()
    dirty = bool(subprocess.run(
        ["git", "status", "--porcelain"], check=True, capture_output=True, text=True,
    ).stdout.strip())
    reports = {
        "tool_contracts": _junit(args.contracts_report),
        "mysql_contracts": _junit(args.mysql_report),
        "real_fullstack": _junit(args.real_report),
    }
    service_text = args.service_list.read_text(encoding="utf-8")
    if "mock" in service_text.casefold():
        raise SystemExit("service list contains a forbidden substitute service")
    manifest = {
        "schema": "myclaw.platform_tool_v3.release_evidence.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "commit_sha": commit_sha,
        "worktree_dirty": dirty,
        "model": {"id": args.model, "endpoint": args.endpoint},
        "credential": "administrator write-only submission; value intentionally not collected",
        "reports": reports,
        "schema_contract": {"path": str(args.schema_contract), "sha256": _digest(args.schema_contract)},
        "database_facts": {"path": str(args.database_facts), "sha256": _digest(args.database_facts)},
        "service_list": {"path": str(args.service_list), "sha256": _digest(args.service_list)},
        "browser_evidence": _files(args.browser_evidence_dir),
        "artifact_checksums": {"path": str(args.artifact_checksums), "sha256": _digest(args.artifact_checksums)},
        "passed": all(report["passed"] for report in reports.values()) and not dirty,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    print(args.output)
    return 0 if manifest["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
