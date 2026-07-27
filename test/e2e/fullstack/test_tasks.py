"""Full-stack: Task board API with real worker."""

import pytest

from e2e.fullstack.conftest import requires_fullstack
from shared.fullstack_helpers import seed_ready_agent

pytestmark = [pytest.mark.fullstack, requires_fullstack]


@pytest.mark.asyncio
async def test_create_task_and_wait_completion(fs_client):
    agent = await seed_ready_agent(fs_client)
    task = await fs_client.create_task(
        agent["id"],
        input_data={"description": "Summarize the project status"},
        title="Fullstack test task",
    )
    assert task["status"] in ("pending", "running")

    completed = await fs_client.wait_for_task(task["id"], timeout=120.0)
    assert completed["status"] == "completed"
    assert completed.get("output_data")

    tasks = await fs_client.list_tasks(status="completed")
    assert any(t["id"] == task["id"] for t in tasks)
