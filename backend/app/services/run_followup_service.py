"""Transactional follow-ups created with terminal Run settlement."""

from app.models.agent_v1 import AgentAssemblyVersion
from app.models.registry import PolicyDefinition
from app.persistence.reliable_writes import OutboxWriter
from app.services.notification_delivery_service import create_notification_delivery_if_channel


async def enqueue_run_terminal_followups(db, *, run, runner, completion_event) -> None:
    await create_notification_delivery_if_channel(
        db, session_id=run.session_id, triggering_event_id=completion_event.id,
        event_type="run_failed" if run.outcome == "failed" else "run_completed",
        run_id=run.id,
    )
    version = await db.get(AgentAssemblyVersion, runner.agent_version_id)
    policy = await db.get(
        PolicyDefinition, version.memory_write_policy_id,
    ) if version and version.memory_write_policy_id else None
    if policy is None or not policy.enabled or policy.policy_key == "memory_write.none":
        return
    await OutboxWriter(db).enqueue(
        topic="runtime.maintenance", action="extract_memory",
        aggregate_type="run", aggregate_id=run.id, partition_key=run.session_id,
        dedupe_key=f"transition:run:{run.id}:extract_memory",
        payload_ref={"run_id": run.id, "runner_id": runner.id,
                     "policy_definition_id": policy.id},
    )
