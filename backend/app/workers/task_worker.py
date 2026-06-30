import asyncio
from app.workers.celery_app import celery_app


@celery_app.task(bind=True, max_retries=3, default_retry_delay=5)
def execute_task(self, task_id: str) -> dict:
    return asyncio.get_event_loop().run_until_complete(_run_task(task_id))


@celery_app.task(bind=True)
def execute_plan_task(self, task_id: str, plan: dict) -> dict:
    return asyncio.get_event_loop().run_until_complete(_run_plan_task(task_id, plan))


async def _run_task(task_id: str) -> dict:
    from app.database import AsyncSessionLocal
    from app.models.task import Task
    from app.models.agent import Agent
    from app.models.session import Session, Runner
    from app.services.agent_service import AgentService
    from app.runtime.agent_runtime import AgentRuntime
    from sqlalchemy import select
    from datetime import datetime
    import uuid

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()
        if not task:
            return {"error": "Task not found"}

        task.status = "running"
        task.started_at = datetime.utcnow()
        await db.commit()

        try:
            session_id = task.session_id
            if not session_id:
                session = Session(
                    id=str(uuid.uuid4()),
                    agent_id=task.agent_id,
                    user_id=task.user_id,
                    mode="chat",
                )
                db.add(session)
                await db.flush()
                task.session_id = session.id
                session_id = session.id
            else:
                res = await db.execute(select(Session).where(Session.id == session_id))
                session = res.scalar_one()

            snapshot = await AgentService.get_snapshot(db, task.agent_id, task.agent_version)

            runner = Runner(
                id=str(uuid.uuid4()),
                session_id=session_id,
                agent_id=task.agent_id,
                agent_version=task.agent_version,
                user_id=task.user_id,
                input_data=task.input_data,
            )
            db.add(runner)
            await db.flush()

            runtime = AgentRuntime(snapshot=snapshot, db=db, runner=runner, session=session)
            user_input = (task.input_data or {}).get("description", str(task.input_data or ""))
            full_output = ""
            async for event in runtime.run(user_input):
                if event.get("type") == "done":
                    full_output = event.get("content", "")

            task.status = "completed"
            task.completed_at = datetime.utcnow()
            task.output_data = {"content": full_output}
            await db.commit()

            # Send to output channel
            if task.output_channel != "platform_ui":
                from app.core.channels.dispatcher import ChannelDispatcher
                await ChannelDispatcher.send(task.output_channel, full_output, db=db)

            # Check if parent task is waiting for this one
            if task.parent_task_id:
                await _maybe_resume_parent(db, task.parent_task_id)

            return {"task_id": task_id, "status": "completed"}

        except Exception as e:
            task.status = "failed"
            task.output_data = {"error": str(e)}
            task.completed_at = datetime.utcnow()
            await db.commit()
            return {"task_id": task_id, "status": "failed", "error": str(e)}


async def _run_plan_task(task_id: str, plan: dict) -> dict:
    from app.database import AsyncSessionLocal
    from app.models.task import Task
    from app.models.session import Session, Runner
    from app.services.agent_service import AgentService
    from app.runtime.agent_runtime import AgentRuntime
    from sqlalchemy import select
    from datetime import datetime
    import uuid

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()
        if not task:
            return {"error": "Task not found"}

        task.status = "running"
        task.started_at = datetime.utcnow()
        await db.commit()

        try:
            res = await db.execute(select(Session).where(Session.id == task.session_id))
            session = res.scalar_one()
            snapshot = await AgentService.get_snapshot(db, task.agent_id, task.agent_version)

            outputs = []
            for step in plan.get("steps", []):
                step["status"] = "running"
                runner = Runner(
                    id=str(uuid.uuid4()),
                    session_id=session.id,
                    agent_id=task.agent_id,
                    agent_version=task.agent_version,
                    user_id=task.user_id,
                    input_data={"step": step["description"]},
                )
                db.add(runner)
                await db.flush()

                runtime = AgentRuntime(snapshot=snapshot, db=db, runner=runner, session=session)
                step_output = ""
                async for event in runtime.run(step["description"]):
                    if event.get("type") == "done":
                        step_output = event.get("content", "")
                step["status"] = "completed"
                step["output"] = step_output
                outputs.append(step_output)
                await db.commit()

            full_output = "\n\n".join(outputs)
            task.status = "completed"
            task.completed_at = datetime.utcnow()
            task.output_data = {"steps": plan["steps"], "summary": full_output}
            await db.commit()
            return {"task_id": task_id, "status": "completed"}

        except Exception as e:
            task.status = "failed"
            task.output_data = {"error": str(e)}
            await db.commit()
            return {"task_id": task_id, "status": "failed", "error": str(e)}


async def _maybe_resume_parent(db, parent_task_id: str):
    """If all sibling tasks of a parent are done, resume the parent."""
    from app.models.task import Task
    from sqlalchemy import select
    result = await db.execute(select(Task).where(Task.id == parent_task_id))
    parent = result.scalar_one_or_none()
    if parent and parent.status == "waiting":
        parent.status = "running"
        parent.waiting_reason = None
        await db.commit()
        execute_task.delay(parent.id)
