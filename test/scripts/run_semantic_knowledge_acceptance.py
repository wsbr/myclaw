#!/usr/bin/env python3
"""Real Bailian Embedding -> durable Knowledge worker -> Qdrant acceptance."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
import uuid

import httpx


def request(client: httpx.Client, method: str, path: str, **kwargs) -> dict:
    response = client.request(method, path, **kwargs)
    if response.is_error:
        raise RuntimeError(
            f"{method} {path} -> {response.status_code}: {response.text[:1200]}"
        )
    return response.json()


def idem() -> str:
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
        "MYCLAW_REAL_LLM_API_KEY": os.getenv("MYCLAW_REAL_LLM_API_KEY"),
        "MYCLAW_REAL_LLM_ENDPOINT": os.getenv(
            "MYCLAW_REAL_LLM_ENDPOINT",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
        ),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise SystemExit(f"Missing required environment variables: {', '.join(missing)}")
    model_name = os.getenv("MYCLAW_REAL_EMBEDDING_MODEL", "text-embedding-v4")
    dimensions = int(os.getenv("MYCLAW_REAL_EMBEDDING_DIMENSIONS", "1024"))
    payload = {
        "schema": "myclaw.semantic_knowledge_acceptance.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "commit_sha": args.commit_sha,
        "target": {
            "provider": "openai_compatible",
            "endpoint": required["MYCLAW_REAL_LLM_ENDPOINT"],
            "model": model_name,
            "dimensions": dimensions,
            "credential_reused_from_chat_provider": True,
            "vector_backend": "qdrant",
        },
        "passed": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with httpx.Client(
            base_url=args.base_url.rstrip("/"), timeout=180, trust_env=False,
        ) as client:
            login = request(client, "POST", "/api/v1/auth/login", json={
                "email": required["INIT_ADMIN_EMAIL"],
                "password": required["INIT_ADMIN_PASSWORD"],
            })
            headers = {"Authorization": f"Bearer {login['access_token']}"}
            model_key = "knowledge.embedding.bailian"
            models = request(
                client, "GET", "/api/v1/admin/models",
                headers=headers, params={"limit": 100},
            )["items"]
            logical = next((item for item in models if item["key"] == model_key), None)
            if logical is None:
                logical = request(
                    client, "POST", "/api/v1/admin/models",
                    headers={**headers, "Idempotency-Key": idem()},
                    json={"key": model_key, "name": "Bailian text-embedding-v4"},
                )
            versions = request(
                client, "GET", f"/api/v1/admin/models/{logical['id']}/versions",
                headers=headers, params={"limit": 50},
            )["items"]
            version = next((item for item in versions if (
                item["provider"] == "openai_compatible"
                and item["model_name"] == model_name
                and item["endpoint"] == required["MYCLAW_REAL_LLM_ENDPOINT"]
                and item["capabilities"].get("model_kind") == "embedding"
                and item["capabilities"].get("embedding_dimensions") == dimensions
            )), None)
            if version is None:
                logical = request(
                    client, "GET", f"/api/v1/admin/models/{logical['id']}",
                    headers=headers,
                )
                version = request(
                    client, "POST", f"/api/v1/admin/models/{logical['id']}/versions",
                    headers={
                        **headers, "Idempotency-Key": idem(),
                        "If-Match": f'W/"{logical["version"]}"',
                    },
                    json={
                        "provider": "openai_compatible",
                        "model_name": model_name,
                        "endpoint": required["MYCLAW_REAL_LLM_ENDPOINT"],
                        "api_key": required["MYCLAW_REAL_LLM_API_KEY"],
                        "default_parameters": {
                            "dimensions": dimensions, "encoding_format": "float",
                        },
                        "capabilities": {
                            "model_kind": "embedding",
                            "embedding_dimensions": dimensions,
                            "embedding_max_batch_size": 10,
                        },
                    },
                )
            conformance = version.get("latest_conformance")
            if not conformance or conformance["status"] != "passed":
                conformance = request(
                    client, "POST",
                    f"/api/v1/admin/models/{logical['id']}/versions/{version['id']}/conformance",
                    headers={**headers, "Idempotency-Key": idem()},
                )
            if conformance["status"] != "passed":
                raise RuntimeError("real Embedding Conformance failed")
            logical = request(
                client, "GET", f"/api/v1/admin/models/{logical['id']}", headers=headers,
            )
            if logical["current_version_id"] != version["id"]:
                logical = request(
                    client, "POST",
                    f"/api/v1/admin/models/{logical['id']}/versions/{version['id']}/activate",
                    headers={
                        **headers, "Idempotency-Key": idem(),
                        "If-Match": f'W/"{logical["version"]}"',
                    },
                )

            base_name = f"Semantic Acceptance {args.commit_sha[:12]}"
            base = request(
                client, "POST", "/api/v1/knowledge-bases", headers=headers,
                json={
                    "name": base_name,
                    "description": "Real Bailian Embedding and Qdrant acceptance evidence",
                    "agent_ids": [], "embedding_model_id": logical["id"],
                },
            )
            if base.get("embedding_model_id") != logical["id"]:
                base = request(
                    client, "PATCH",
                    f"/api/v1/knowledge-bases/{base['id']}/embedding-model",
                    headers=headers, json={"embedding_model_id": logical["id"]},
                )
            body = (
                "Recovery policy evidence. If the payment node crashes, resume from the "
                "last durable checkpoint and verify the fencing token before settlement. "
                "Never restart the payment workflow from the beginning. "
                f"Acceptance commit {args.commit_sha}."
            ).encode()
            ingest = request(
                client, "POST", f"/api/v1/knowledge-bases/{base['id']}/documents",
                headers=headers,
                files={"file": ("recovery-policy.txt", body, "text/plain")},
            )
            deadline = time.monotonic() + 180
            while True:
                base = request(
                    client, "GET", f"/api/v1/knowledge-bases/{base['id']}",
                    headers=headers,
                )
                pending = base.get("pending_index_revision")
                current = base.get("current_index_revision")
                if pending and pending["status"] == "failed":
                    raise RuntimeError(
                        f"semantic index failed: {pending.get('error_code')}"
                    )
                if (
                    pending is None and current
                    and current["vector_backend"] == "qdrant"
                    and current["vector_point_count"] > 0
                ):
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("semantic index did not become ready within 180 seconds")
                time.sleep(1)
            search = request(
                client, "POST", f"/api/v1/knowledge-bases/{base['id']}/search",
                headers=headers,
                json={"query": "服务器宕机后怎样避免重复扣款？", "limit": 5},
            )
            if not search["results"]:
                raise RuntimeError("semantic query returned no citations")
            top = search["results"][0]
            if top["retrieval_kind"] != "semantic":
                raise RuntimeError("query did not use the semantic retrieval path")
            if top["index_revision_id"] != current["id"]:
                raise RuntimeError("citation did not bind the current semantic revision")
            payload.update({
                "model": {
                    "id": logical["id"], "current_version_id": version["id"],
                    "conformance_suite": conformance["suite_version"],
                    "conformance_status": conformance["status"],
                },
                "knowledge_base": {
                    "id": base["id"], "index_revision_id": current["id"],
                    "vector_backend": current["vector_backend"],
                    "vector_point_count": current["vector_point_count"],
                },
                "ingest": {
                    "source_checksum": ingest["source"]["checksum"],
                    "idempotent_replay": ingest["idempotent_replay"],
                },
                "search": {
                    "query": search["query"], "count": search["count"],
                    "top_result": {
                        key: top[key] for key in (
                            "index_revision_id", "source_checksum", "chunk_checksum",
                            "score", "lexical_score", "semantic_score", "retrieval_kind",
                        )
                    },
                },
                "passed": True,
            })
    except Exception as error:
        payload["error"] = {
            "type": type(error).__name__, "message": str(error)[:2000],
        }
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
