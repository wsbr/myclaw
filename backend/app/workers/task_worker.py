import asyncio
import concurrent.futures
import uuid
from datetime import datetime

from app.workers.celery_app import celery_app


def _run_coro(factory):
    """Run async coroutine from sync Celery task with an isolated event loop + DB engine.

    Each invocation gets a fresh asyncio loop and SQLAlchemy engine to avoid
    'attached to a different loop' errors when Celery reuses worker threads.
    `factory` must be a callable returning a new coroutine (not a coroutine object).
    """
    def _run_in_thread():
        async def _wrapper():
            import app.database as db_module
            from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
            from app.config import settings

            engine = create_async_engine(settings.database_url, pool_pre_ping=True)
            db_module.engine = engine
            db_module.AsyncSessionLocal = async_sessionmaker(
                engine, class_=AsyncSession, expire_on_commit=False
            )
            try:
                return await factory()
            finally:
                await engine.dispose()

        return asyncio.run(_wrapper())

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _run_in_thread()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_run_in_thread).result()


@celery_app.task(bind=True, max_retries=3, default_retry_delay=5)
def execute_task(self, task_id: str) -> dict:
    return _run_coro(lambda: _run_task(task_id))


@celery_app.task(bind=True)
def execute_plan_task(self, task_id: str, plan: dict) -> dict:
    return _run_coro(lambda: _run_plan_task(task_id, plan))


async def _run_task(task_id: str) -> dict:
    from app.database import AsyncSessionLocal
    from app.models.task import Task
    from app.models.session import Session, Runner
    from app.services.agent_service import AgentService
    from app.runtime.agent_runtime import AgentRuntime
    from app.runtime.context_builder import SCHEDULED_TASK_EXECUTION_CONTEXT
    from sqlalchemy import select

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

            runtime = AgentRuntime(snapshot=snapshot, db=db, runner=runner, session=session, task_id=task_id)
            user_input = (task.input_data or {}).get("description", str(task.input_data or ""))
            execution_context = SCHEDULED_TASK_EXECUTION_CONTEXT if task.trigger_type == "scheduled" else None
            full_output = ""
            async for event in runtime.run(user_input, execution_context=execution_context):
                if event.get("type") == "done":
                    full_output = event.get("content", "")

            task.status = "completed"
            task.completed_at = datetime.utcnow()
            task.output_data = {"content": full_output}
            await db.commit()

            from app.core.channels.dispatcher import ChannelDispatcher
            from app.core.tool_registry.base import RuntimeContext

            context = RuntimeContext(
                session_id=session_id,
                runner_id=runner.id,
                task_id=task_id,
                user_id=task.user_id,
                agent_id=task.agent_id,
                output_channel=task.output_channel,
            )
            await ChannelDispatcher.send(
                task.output_channel,
                full_output,
                context=context,
                agent_id=task.agent_id,
                db=db,
            )

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
    from app.models.session import Session, Runner, ExecutionPlan
    from app.services.agent_service import AgentService
    from app.runtime.agent_runtime import AgentRuntime
    from app.runtime.tracer import Tracer
    from app.runtime.plan_executor import (
        sort_plan_steps,
        build_step_prompt,
        broadcast_session_event,
        normalize_steps,
    )
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()
        if not task:
            return {"error": "Task not found"}

        task.status = "running"
        task.started_at = datetime.utcnow()
        await db.commit()

        plan_id = plan.get("plan_id")
        goal = plan.get("goal", "Plan execution")
        steps = normalize_steps(plan.get("steps", []))
        ordered_steps = sort_plan_steps(steps)

        try:
            res = await db.execute(select(Session).where(Session.id == task.session_id))
            session = res.scalar_one()
            snapshot = await AgentService.get_snapshot(db, task.agent_id, task.agent_version)

            plan_record = None
            if plan_id:
                plan_res = await db.execute(select(ExecutionPlan).where(ExecutionPlan.id == plan_id))
                plan_record = plan_res.scalar_one_or_none()
                if plan_record:
                    plan_record.status = "running"
                    await db.commit()

            parent_runner = Runner(
                id=str(uuid.uuid4()),
                session_id=session.id,
                agent_id=task.agent_id,
                agent_version=task.agent_version,
                user_id=task.user_id,
                input_data={"type": "plan", "plan_id": plan_id, "goal": goal, "steps": ordered_steps},
                status="running",
                started_at=datetime.utcnow(),
            )
            db.add(parent_runner)
            await db.flush()

            parent_tracer = Tracer(db=db, runner=parent_runner)
            step_outputs: dict[str, str] = {}
            outputs: list[str] = []

            for step in ordered_steps:
                step["status"] = "running"
                if plan_record:
                    plan_record.plan_json = {"goal": goal, "steps": ordered_steps}
                    await db.commit()

                await broadcast_session_event(session.id, "plan_step_update", {
                    "plan_id": plan_id,
                    "task_id": task_id,
                    "step_id": step["id"],
                    "status": "running",
                    "steps": ordered_steps,
                })

                plan_step_span = await parent_tracer.start_span(
                    span_type="plan_step",
                    name=f"Step: {step['id']}",
                    snapshot={"step": step, "goal": goal},
                )

                child_runner = Runner(
                    id=str(uuid.uuid4()),
                    session_id=session.id,
                    agent_id=task.agent_id,
                    agent_version=task.agent_version,
                    user_id=task.user_id,
                    parent_runner_id=parent_runner.id,
                    input_data={"step_id": step["id"], "description": step["description"]},
                )
                db.add(child_runner)
                await db.flush()

                step_prompt = build_step_prompt(step, step_outputs, goal)
                runtime = AgentRuntime(snapshot=snapshot, db=db, runner=child_runner, session=session, task_id=task_id)
                step_output = ""
                try:
                    async for event in runtime.run(step_prompt):
                        if event.get("type") == "done":
                            step_output = event.get("content", "")
                except Exception as step_err:
                    step["status"] = "failed"
                    step["output"] = str(step_err)
                    await parent_tracer.end_span(plan_step_span, error=str(step_err))
                    if plan_record:
                        plan_record.status = "failed"
                        plan_record.plan_json = {"goal": goal, "steps": ordered_steps}
                    task.status = "failed"
                    task.completed_at = datetime.utcnow()
                    task.output_data = {"steps": ordered_steps, "error": str(step_err)}
                    await db.commit()
                    await broadcast_session_event(session.id, "plan_step_update", {
                        "plan_id": plan_id,
                        "task_id": task_id,
                        "step_id": step["id"],
                        "status": "failed",
                        "steps": ordered_steps,
                    })
                    await broadcast_session_event(session.id, "plan_completed", {
                        "plan_id": plan_id,
                        "task_id": task_id,
                        "status": "failed",
                    })
                    return {"task_id": task_id, "status": "failed", "error": str(step_err)}

                step["status"] = "completed"
                step["output"] = step_output
                step_outputs[step["id"]] = step_output
                outputs.append(step_output)

                await parent_tracer.end_span(
                    plan_step_span,
                    request={"prompt": step_prompt},
                    response={"output": step_output},
                )

                task.output_data = {"steps": ordered_steps, "summary": "\n\n".join(outputs)}
                if plan_record:
                    plan_record.plan_json = {"goal": goal, "steps": ordered_steps}
                await db.commit()

                await broadcast_session_event(session.id, "plan_step_update", {
                    "plan_id": plan_id,
                    "task_id": task_id,
                    "step_id": step["id"],
                    "status": "completed",
                    "output": step_output,
                    "steps": ordered_steps,
                })

            full_output = "\n\n".join(outputs)
            await parent_tracer.end_runner(output={"steps": ordered_steps, "summary": full_output})

            task.status = "completed"
            task.completed_at = datetime.utcnow()
            task.output_data = {"steps": ordered_steps, "summary": full_output}
            if plan_record:
                plan_record.status = "completed"
                plan_record.plan_json = {"goal": goal, "steps": ordered_steps}
            await db.commit()

            if task.output_channel != "platform_ui":
                from app.core.channels.dispatcher import ChannelDispatcher
                await ChannelDispatcher.send(task.output_channel, full_output, db=db)
            else:
                from app.services.notification_service import NotificationService
                await NotificationService.create(
                    db,
                    user_id=task.user_id,
                    content=full_output,
                    agent_id=task.agent_id,
                    session_id=session.id,
                    task_id=task_id,
                    title=task.title or "计划执行完成",
                )

            await broadcast_session_event(session.id, "plan_completed", {
                "plan_id": plan_id,
                "task_id": task_id,
                "status": "completed",
                "summary": full_output,
                "steps": ordered_steps,
            })
            return {"task_id": task_id, "status": "completed"}

        except Exception as e:
            task.status = "failed"
            task.output_data = {"error": str(e), "steps": ordered_steps}
            task.completed_at = datetime.utcnow()
            if plan_id:
                plan_res = await db.execute(select(ExecutionPlan).where(ExecutionPlan.id == plan_id))
                plan_record = plan_res.scalar_one_or_none()
                if plan_record:
                    plan_record.status = "failed"
            await db.commit()
            await broadcast_session_event(task.session_id or "", "plan_completed", {
                "plan_id": plan_id,
                "task_id": task_id,
                "status": "failed",
                "error": str(e),
            })
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
