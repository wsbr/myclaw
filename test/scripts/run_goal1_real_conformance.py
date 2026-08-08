#!/usr/bin/env python3
"""Run and persist the real target Provider ConformanceRun through the admin API."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import uuid

import httpx


def _request(client: httpx.Client, method: str, path: str, **kwargs) -> dict:
    response = client.request(method, path, **kwargs)
    if response.is_error:
        raise RuntimeError(f"{method} {path} -> {response.status_code}: {response.text[:2000]}")
    return response.json()


def _idem() -> str:
    return str(uuid.uuid4())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8101")
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    required = {
        "INIT_ADMIN_EMAIL": os.getenv("INIT_ADMIN_EMAIL"),
        "INIT_ADMIN_PASSWORD": os.getenv("INIT_ADMIN_PASSWORD"),
        "MYCLAW_REAL_LLM_ENDPOINT": os.getenv("MYCLAW_REAL_LLM_ENDPOINT"),
        "MYCLAW_REAL_LLM_MODEL": os.getenv("MYCLAW_REAL_LLM_MODEL"),
        "MYCLAW_REAL_LLM_API_KEY": os.getenv("MYCLAW_REAL_LLM_API_KEY"),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise SystemExit(f"Missing required environment variables: {', '.join(missing)}")

    payload = {
        "schema": "myclaw.agent_native_v2.goal1_provider_conformance.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "commit_sha": args.commit_sha,
        "target": {
            "provider": "openai_compatible",
            "endpoint": required["MYCLAW_REAL_LLM_ENDPOINT"],
            "model": required["MYCLAW_REAL_LLM_MODEL"],
            "credential": "env://MYCLAW_REAL_LLM_API_KEY",
        },
        "passed": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with httpx.Client(
            base_url=args.base_url.rstrip("/"), timeout=900, trust_env=False,
        ) as client:
            login = _request(client, "POST", "/api/v1/auth/login", json={
                "email": required["INIT_ADMIN_EMAIL"],
                "password": required["INIT_ADMIN_PASSWORD"],
            })
            headers = {"Authorization": f"Bearer {login['access_token']}"}
            model_key = f"goal1-qwen-{args.commit_sha[:12]}"
            models = _request(
                client, "GET", "/api/v1/admin/models",
                headers=headers, params={"limit": 100},
            )["items"]
            logical = next((item for item in models if item["key"] == model_key), None)
            if logical is None:
                logical = _request(
                    client, "POST", "/api/v1/admin/models",
                    headers={**headers, "Idempotency-Key": _idem()},
                    json={"key": model_key, "name": "Goal 1 qwen3.5-plus"},
                )
            logical = _request(
                client, "GET", f"/api/v1/admin/models/{logical['id']}", headers=headers,
            )
            version = _request(
                client, "POST", f"/api/v1/admin/models/{logical['id']}/versions",
                headers={
                    **headers,
                    "Idempotency-Key": _idem(),
                    "If-Match": f'W/"{logical["version"]}"',
                },
                json={
                    "provider": "openai_compatible",
                    "model_name": required["MYCLAW_REAL_LLM_MODEL"],
                    "endpoint": required["MYCLAW_REAL_LLM_ENDPOINT"],
                    "api_key": required["MYCLAW_REAL_LLM_API_KEY"],
                    "default_parameters": {
                        "temperature": 0,
                        "max_tokens": 4096,
                        "extra_body": {"enable_thinking": True},
                    },
                    "capabilities": {
                        "streaming": True,
                        "native_tool_calling": True,
                        "parallel_tool_calling": False,
                        "tool_result_continuation": True,
                        "usage_in_stream": True,
                        "stop_reason": True,
                        "context_window": 16384,
                        "structured_output": "none",
                    },
                },
            )
            conformance = _request(
                client, "POST",
                f"/api/v1/admin/models/{logical['id']}/versions/{version['id']}/conformance",
                headers={**headers, "Idempotency-Key": _idem()},
            )
            payload.update({
                "logical_model": logical,
                "model_version": version,
                "conformance_run": conformance,
            })
            if conformance["status"] == "passed":
                logical = _request(
                    client, "GET", f"/api/v1/admin/models/{logical['id']}", headers=headers,
                )
                activation = _request(
                    client, "POST",
                    f"/api/v1/admin/models/{logical['id']}/versions/{version['id']}/activate",
                    headers={
                        **headers,
                        "Idempotency-Key": _idem(),
                        "If-Match": f'W/"{logical["version"]}"',
                    },
                )
                payload["activation"] = activation
                payload["passed"] = activation["current_version_id"] == version["id"]
    except Exception as error:
        payload["error"] = {"type": type(error).__name__, "message": str(error)[:4000]}

    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
