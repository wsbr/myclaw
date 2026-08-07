#!/usr/bin/env python3
"""Capture read-only evidence for the isolated pre-cutover UAT environment."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


def run(*args: str) -> str:
    return subprocess.run(
        args, check=True, text=True, stdout=subprocess.PIPE,
    ).stdout.strip()


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] not in {"before", "after"}:
        raise SystemExit("usage: collect_legacy_uat_snapshot.py before|after /absolute/output.json")
    phase = sys.argv[1]
    target = Path(sys.argv[2]).resolve()
    container = os.getenv("MYCLAW_LEGACY_UAT_MYSQL_CONTAINER", "myclaw-uat-mysql-1")
    sql = r"""
      SET SESSION group_concat_max_len=67108864;
      SELECT CONCAT('alembic_head=',version_num) FROM alembic_version;
      SELECT CONCAT('run_count=',COUNT(*)) FROM run;
      SELECT CONCAT('failed_run_count=',COUNT(*)) FROM run
       WHERE lifecycle='completed' AND outcome='failed';
      SELECT CONCAT('failed_fact_hash=',COALESCE(SHA2(GROUP_CONCAT(
        SHA2(CONCAT_WS(CHAR(31),id,session_id,user_id,mode,execution_kind,lifecycle,
          COALESCE(wait_reason,''),COALESCE(outcome,''),entry_agent_id,
          COALESCE(source_run_id,''),COALESCE(result_summary,''),COALESCE(error_code,''),
          version,DATE_FORMAT(started_at,'%Y-%m-%dT%H:%i:%s.%f'),
          COALESCE(DATE_FORMAT(finished_at,'%Y-%m-%dT%H:%i:%s.%f'),''),
          DATE_FORMAT(created_at,'%Y-%m-%dT%H:%i:%s.%f'),
          DATE_FORMAT(updated_at,'%Y-%m-%dT%H:%i:%s.%f'),status,
          COALESCE(root_work_item_id,''),permission_mode,CAST(hard_budget_snapshot AS CHAR),
          COALESCE(DATE_FORMAT(completed_at,'%Y-%m-%dT%H:%i:%s.%f'),'')),256)
        ORDER BY id SEPARATOR ''),256),SHA2('',256))) FROM run
       WHERE lifecycle='completed' AND outcome='failed';
    """
    raw = run(
        "docker", "exec", container, "sh", "-c",
        'MYSQL_PWD="$MYSQL_PASSWORD" mysql -N -u"$MYSQL_USER" "$MYSQL_DATABASE" -e "$1"',
        "readonly-snapshot", sql,
    )
    facts = {}
    for line in raw.splitlines():
        key, value = line.split("=", 1)
        facts[key] = int(value) if key.endswith("count") else value
    inspect = json.loads(run("docker", "inspect", container))[0]
    project = inspect.get("Config", {}).get("Labels", {}).get("com.docker.compose.project")
    if project != "myclaw-uat":
        raise RuntimeError(f"refusing unexpected legacy Compose project: {project!r}")
    payload = {
        "schema": "myclaw.legacy_uat_readonly_snapshot.v1",
        "phase": phase,
        "access": "read_only_queries_only",
        "container_id": inspect["Id"],
        "image_id": inspect["Image"],
        "compose_project": project,
        "mounts": sorted(
            [
                {"name": item.get("Name"), "destination": item.get("Destination")}
                for item in inspect.get("Mounts", []) if item.get("Type") == "volume"
            ],
            key=lambda item: (item["name"] or "", item["destination"] or ""),
        ),
        "facts": facts,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
