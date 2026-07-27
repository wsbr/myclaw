import json
from typing import AsyncIterator
from datetime import datetime, date
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.session import Runner, Session
from app.models.tool import ModelProvider, Skill
from app.models.agent import AgentTool, AgentSkill, AgentBuiltinTool
from app.runtime.tracer import Tracer
from app.runtime.context_builder import ContextBuilder
from app.core.model_hub.registry import ModelHub
from app.core.model_hub.base import TokenUsage
from app.core.tool_registry.registry import ToolRegistry
from app.core.tool_registry.base import RuntimeContext


class AgentRuntime:
    def __init__(self, snapshot: dict, db: AsyncSession, runner: Runner, session: Session, task_id: str | None = None):
        self.snapshot = snapshot
        self.db = db
        self.runner = runner
        self.session = session
        self.task_id = task_id
        self.tracer = Tracer(db=db, runner=runner)
        self.ctx_builder = ContextBuilder(snapshot)

    async def run(self, user_input: str, execution_context: str | None = None) -> AsyncIterator[dict]:
        self.runner.status = "running"
        self.runner.started_at = datetime.utcnow()
        await self.db.flush()

        model_cfg = self.snapshot.get("model_config_") or self.snapshot.get("model_config") or {}
        skills = await self._load_skills()
        builtin_keys = await self._load_builtin_tool_keys()
        group_agents = await self._load_group_agents() if "group" in (self.snapshot.get("modes") or []) else []

        system_prompt = self.ctx_builder.render_system_prompt(
            skills=skills,
            group_agents=group_agents,
            execution_context=execution_context,
        )
        tools = ToolRegistry.get_definitions(builtin_keys)

        history_messages = await self._load_history()
        base_messages = history_messages + [{"role": "user", "content": user_input}]
        messages = self.ctx_builder.apply_context_strategy(base_messages)

        llm = await self._get_llm(model_cfg)

        context = RuntimeContext(
            session_id=self.session.id,
            runner_id=self.runner.id,
            task_id=self.task_id,
            user_id=self.runner.user_id,
            agent_id=self.session.agent_id,
        )

        params = {k: v for k, v in model_cfg.items() if k not in ("provider_id", "provider")}
        params["system"] = system_prompt

        full_response = ""
        llm_call_idx = 0

        try:
            while True:
                llm_call_idx += 1
                span = await self.tracer.start_span(
                    span_type="llm_call",
                    name=f"LLM Call #{llm_call_idx}",
                    snapshot={"system_prompt": system_prompt, "messages": messages, "tools": tools},
                )

                chunk_text = ""
                tool_calls_raw = []
                usage: TokenUsage | None = None

                async for chunk in llm.stream(messages, tools or None, dict(params)):
                    if chunk.type == "text_delta" and chunk.content:
                        chunk_text += chunk.content
                        yield {"type": "stream_chunk", "content": chunk.content}
                    elif chunk.type == "tool_call_delta" and chunk.tool_call:
                        tool_calls_raw.append(chunk.tool_call)
                    elif chunk.type == "done" and chunk.usage:
                        usage = chunk.usage

                if not tool_calls_raw and not chunk_text:
                    # 兜底：两个适配器都已上报 tool_call_delta + usage 后，这个分支应该很少触发
                    response = await llm.chat(messages, tools or None, dict(params))
                    chunk_text = response.content
                    tool_calls_raw = response.tool_calls
                    usage = response.usage
                    if chunk_text:
                        yield {"type": "stream_chunk", "content": chunk_text}

                if usage:
                    input_tokens = usage.input_tokens
                    output_tokens = usage.output_tokens
                else:
                    input_tokens = await llm.count_tokens(messages)
                    output_tokens = max(len(chunk_text) // 4, 1)

                await self.tracer.end_span(
                    span,
                    request={"messages": messages, "model": model_cfg.get("model_id")},
                    response={"content": chunk_text, "tool_calls": [tc.model_dump() for tc in tool_calls_raw]},
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )

                if not tool_calls_raw:
                    full_response = chunk_text
                    break

                messages.append({"role": "assistant", "content": chunk_text or "", "tool_calls": [
                    {"id": tc.id, "type": "tool_use", "name": tc.name, "input": tc.arguments}
                    for tc in tool_calls_raw
                ]})

                for tc in tool_calls_raw:
                    tool_span = await self.tracer.start_span(
                        span_type="tool_call",
                        name=f"Tool: {tc.name}",
                        snapshot={"tool_name": tc.name, "args": tc.arguments},
                        parent_span_id=span.id,
                    )
                    result = await ToolRegistry.execute(tc.name, tc.arguments, context, db=self.db)
                    await self.tracer.end_span(
                        tool_span,
                        request={"name": tc.name, "args": tc.arguments},
                        response=result.model_dump(),
                    )
                    yield {"type": "tool_result", "tool": tc.name, "success": result.success, "data": result.data}

                    messages.append({
                        "role": "tool",
                        "tool_use_id": tc.id,
                        "content": json.dumps(result.data if result.success else {"error": result.error}),
                    })

            yield {"type": "done", "content": full_response}
            await self.tracer.end_runner(output={"content": full_response})
            await self._write_usage_stats(model_cfg)

        except Exception as e:
            yield {"type": "error", "error": str(e)}
            await self.tracer.end_runner(error=str(e))

    # ------------------------------------------------------------------ #

    async def _load_history(self) -> list[dict]:
        """Load previous runner outputs in this session as message history."""
        from app.models.session import Runner as RunnerModel
        result = await self.db.execute(
            select(RunnerModel)
            .where(
                RunnerModel.session_id == self.session.id,
                RunnerModel.id != self.runner.id,
                RunnerModel.status == "completed",
            )
            .order_by(RunnerModel.created_at)
        )
        history = []
        for r in result.scalars().all():
            if r.input_data:
                user_msg = r.input_data.get("message") or r.input_data.get("description", "")
                if user_msg:
                    history.append({"role": "user", "content": user_msg})
            if r.output_data:
                assistant_msg = r.output_data.get("content", "")
                if assistant_msg:
                    history.append({"role": "assistant", "content": assistant_msg})
        return history

    async def _load_skills(self) -> list[Skill]:
        result = await self.db.execute(
            select(Skill)
            .join(AgentSkill, AgentSkill.skill_id == Skill.id)
            .where(AgentSkill.agent_id == self.session.agent_id)
        )
        return result.scalars().all()

    async def _load_builtin_tool_keys(self) -> list[str]:
        result = await self.db.execute(
            select(AgentBuiltinTool)
            .where(
                AgentBuiltinTool.agent_id == self.session.agent_id,
                AgentBuiltinTool.enabled == True,
            )
        )
        return [row.tool_key for row in result.scalars().all()]

    async def _load_group_agents(self) -> list[dict]:
        from app.models.agent import Agent
        result = await self.db.execute(
            select(Agent).where(
                Agent.status == "active",
                Agent.id != self.session.agent_id,
            )
        )
        agents = []
        for a in result.scalars().all():
            cfg = a.group_config or {}
            if cfg.get("enabled"):
                agents.append({"name": a.name, "description": a.description, "id": a.id})
        return agents

    async def _write_usage_stats(self, model_cfg: dict):
        from app.models.stats import UsageStat
        import uuid
        today = date.today()
        model_id = model_cfg.get("model_id", "unknown")

        result = await self.db.execute(
            select(UsageStat).where(
                UsageStat.agent_id == self.session.agent_id,
                UsageStat.user_id == self.runner.user_id,
                UsageStat.stat_date == today,
            )
        )
        stat = result.scalar_one_or_none()
        if stat is None:
            stat = UsageStat(
                id=str(uuid.uuid4()),
                agent_id=self.session.agent_id,
                user_id=self.runner.user_id,
                stat_date=today,
                runner_count=0, session_count=0, task_count=0,
                tool_call_count=0, input_tokens=0, output_tokens=0,
                total_cost_usd=0.0, avg_latency_ms=0.0, error_count=0,
            )
            self.db.add(stat)

        stat.runner_count = (stat.runner_count or 0) + 1
        stat.input_tokens = (stat.input_tokens or 0) + (self.runner.total_tokens or 0)
        stat.total_cost_usd = (stat.total_cost_usd or 0.0) + float(self.runner.total_cost_usd or 0)
        if self.runner.status == "failed":
            stat.error_count += 1
        if self.runner.duration_ms:
            # rolling average
            prev_avg = stat.avg_latency_ms or 0.0
            n = stat.runner_count
            stat.avg_latency_ms = (prev_avg * (n - 1) + self.runner.duration_ms) / n

        await self.db.commit()

    async def _get_llm(self, model_cfg: dict):
        provider_id = model_cfg.get("provider_id")
        if not provider_id:
            raise ValueError("No provider_id in model_config")
        result = await self.db.execute(select(ModelProvider).where(ModelProvider.id == provider_id))
        provider = result.scalar_one_or_none()
        if not provider:
            raise ValueError(f"Model provider {provider_id} not found")
        return ModelHub.get(
            model_cfg,
            provider_type=provider.provider,
            api_key=provider.api_key_encrypted or "",
            base_url=provider.base_url,
        )
