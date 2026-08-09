"""Real API/database/worker acceptance for immutable Skill packages."""

from __future__ import annotations

import io
import uuid
import zipfile

import pytest


pytestmark = pytest.mark.fullstack


def skill_archive(version: str) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            "SKILL.md",
            (
                "---\nname: exact-evidence-writer\n"
                f"description: Frozen acceptance instructions {version}\n"
                "allowed-tools:\n  - read_file\n---\n"
                f"# Exact marker\nUse marker {version}.\n\n"
                "Read `references/evidence.md` and include its exact evidence in the response."
            ),
        )
        archive.writestr("references/evidence.md", f"evidence-{version}")
    return output.getvalue()


def test_skill_upload_publish_version_and_cache_sync(phase_api, record_testsuite_property):
    logical_name = f"exact-skill-{uuid.uuid4().hex[:10]}"
    created = phase_api.request(
        "POST", "/api/v1/skills", idem=True,
        data={"logical_name": logical_name},
        files={"package": ("evidence.skill", skill_archive("v1"), "application/zip")},
    ).json()
    first = created["uploaded_version"]
    assert first["status"] == "uploaded"
    assert first["archive_type"] == "zip"
    assert first["package_size"] > 0
    assert len(first["checksum"]) == 64
    assert len(first["content_hash"]) == 64
    assert first["manifest"]["declared_metadata"]["allowed-tools"] == ["read_file"]

    published = phase_api.request(
        "POST",
        f"/api/v1/skills/{created['id']}/versions/{first['id']}/publish",
        idem=True,
    ).json()
    assert published["status"] == "published"
    assert published["sync_status"] == "queued"
    persisted_first = phase_api.request(
        "GET", f"/api/v1/skills/{created['id']}/versions",
    ).json()["items"][0]
    republished = phase_api.request(
        "POST",
        f"/api/v1/skills/{created['id']}/versions/{first['id']}/publish",
        idem=True,
    ).json()
    assert republished["published_at"] == persisted_first["published_at"]
    synced = phase_api.request(
        "POST", f"/api/v1/skills/{created['id']}/sync", idem=True,
    ).json()
    assert synced["path_uri"] == f"skill://{created['id']}/{first['checksum']}/"

    second = phase_api.request(
        "POST", f"/api/v1/skills/{created['id']}/versions", idem=True,
        files={"package": ("evidence-v2.zip", skill_archive("v2"), "application/zip")},
    ).json()
    assert second["version_no"] == 2
    phase_api.request(
        "POST",
        f"/api/v1/skills/{created['id']}/versions/{second['id']}/publish",
        idem=True,
    )
    versions = phase_api.request(
        "GET", f"/api/v1/skills/{created['id']}/versions",
    ).json()["items"]
    by_id = {item["id"]: item for item in versions}
    assert by_id[first["id"]]["status"] == "deprecated"
    assert by_id[first["id"]]["checksum"] == first["checksum"]
    assert by_id[second["id"]]["status"] == "published"
    record_testsuite_property(
        "skill_runtime",
        "portable-upload,immutable-version,metadata,checksum,cache-sync",
    )


@pytest.mark.timeout(1200)
def test_skill_model_progressive_reads_manifest_and_reference(
    phase_api, real_llm_world, record_testsuite_property,
):
    marker = f"progressive-{uuid.uuid4().hex[:10]}"
    logical_name = f"progressive-skill-{uuid.uuid4().hex[:10]}"
    created = phase_api.request(
        "POST", "/api/v1/skills", idem=True,
        data={"logical_name": logical_name},
        files={"package": ("progressive.skill", skill_archive(marker), "application/zip")},
    ).json()
    version = phase_api.request(
        "POST",
        f"/api/v1/skills/{created['id']}/versions/{created['uploaded_version']['id']}/publish",
        idem=True,
    ).json()
    phase_api.request("POST", f"/api/v1/skills/{created['id']}/sync", idem=True)

    read_file_id = next(
        item["id"] for item in real_llm_world["tools"]
        if item["tool_key"] == "read_file" and item["enabled"]
    )
    policy = real_llm_world["policies"]
    agent = phase_api.request("POST", "/api/v1/agents", json={
        "visibility": "enterprise",
        "draft": {
            "name": f"Progressive Skill Reader {marker}",
            "description": "Reads frozen Skill instructions progressively.",
            "instructions": (
                "When the request matches the bound Skill, first read its SKILL.md path "
                "from the catalog, then read only the referenced file needed. Return exact markers."
            ),
            "model_id": real_llm_world["model"]["id"],
            "sandbox_profile_id": real_llm_world["sandbox"]["id"],
            "context_policy_id": policy["context.standard"],
            "history_policy_id": policy["history.recent"],
            "memory_write_policy_id": policy["memory_write.none"],
            "memory_recall_policy_id": policy["memory_recall.none"],
            "group_enabled": False, "tool_ids": [read_file_id],
            "skill_ids": [created["id"]], "bound_agent_ids": [],
            "discovery_config": {"domain_tags": ["progressive-skill"]},
            "collaboration_config": {"accepts_delegation": False},
        },
    }).json()
    published = phase_api.request(
        "POST", f"/api/v1/agents/{agent['agent']['id']}/publish", idem=True,
        json={"expected_draft_revision": agent["draft"]["draft_revision"]},
    ).json()["published_version"]
    assert published["skills"][0]["skill_version_id"] == version["id"]
    assert published["skills"][0]["checksum"] == version["checksum"]

    session = phase_api.create_session(agent["agent"]["id"])
    command = phase_api.chat(
        session["id"],
        "Use the bound exact-evidence-writer Skill and return both its exact marker and reference evidence.",
    )
    run = phase_api.wait_run(command["run"]["id"], timeout=900)
    assert run["status"] == "completed", run
    calls = [
        item for item in phase_api.invocations(run["id"])
        if item["tool_key"] == "read_file" and item["status"] == "succeeded"
    ]
    paths = [item["arguments"]["path"] for item in calls]
    assert len(paths) >= 2, calls
    assert any(path.endswith("/SKILL.md") for path in paths)
    assert any(path.endswith("/references/evidence.md") for path in paths)
    messages = phase_api.request(
        "GET", f"/api/v1/sessions/{session['id']}/messages",
    ).json()["items"]
    answer = next(item["content"] for item in reversed(messages) if item["role"] == "assistant")
    assert marker in answer
    assert f"evidence-{marker}" in answer
    record_testsuite_property(
        "skill_progressive_disclosure",
        "catalog-metadata,read-SKILL.md,read-reference,frozen-checksum",
    )
