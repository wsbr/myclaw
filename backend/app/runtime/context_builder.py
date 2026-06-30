from __future__ import annotations
from typing import Any


class ContextBuilder:
    """
    负责将 Agent 快照渲染成最终发送给 LLM 的 system prompt，
    并根据 context_strategy 对 messages 历史做裁剪/压缩。
    """

    def __init__(self, snapshot: dict):
        self.snapshot = snapshot
        self.strategy: dict = snapshot.get("context_strategy") or {
            "window_type": "sliding",
            "max_tokens": 128000,
            "compression_threshold": 0.8,
            "compression_method": "truncate",
        }

    # ------------------------------------------------------------------ #
    # System prompt                                                        #
    # ------------------------------------------------------------------ #

    def render_system_prompt(
        self,
        skills: list[Any] | None = None,
        tool_descriptions: list[str] | None = None,
        group_agents: list[dict] | None = None,
    ) -> str:
        blocks = self.snapshot.get("system_prompt_blocks") or {}
        parts: list[str] = []

        if identity := blocks.get("identity", "").strip():
            parts.append(f"# Identity\n{identity}")

        if instructions := blocks.get("instructions", "").strip():
            parts.append(f"# Instructions\n{instructions}")

        if skills:
            skill_lines = "\n\n".join(
                f"## {getattr(s, 'display_name', s.name)}\n{s.content}"
                for s in skills
            )
            parts.append(f"# Skills\n{skill_lines}")

        if tool_descriptions:
            parts.append("# Available Tools\n" + "\n".join(f"- {t}" for t in tool_descriptions))

        if group_agents:
            agent_lines = "\n".join(
                f"- **{a['name']}**: {a['description']}" for a in group_agents
            )
            parts.append(f"# Collaborating Agents (Group Mode)\n{agent_lines}")

        if context := blocks.get("context", "").strip():
            parts.append(f"# Context\n{context}")

        if output_format := blocks.get("output_format", "").strip():
            parts.append(f"# Output Format\n{output_format}")

        return "\n\n".join(parts)

    # ------------------------------------------------------------------ #
    # Message history windowing                                            #
    # ------------------------------------------------------------------ #

    def apply_context_strategy(self, messages: list[dict]) -> list[dict]:
        window_type = self.strategy.get("window_type", "sliding")
        max_tokens = self.strategy.get("max_tokens", 128000)

        if window_type == "full":
            return messages

        if window_type == "sliding":
            return self._sliding_window(messages, max_tokens)

        if window_type == "summary":
            return self._summary_window(messages, max_tokens)

        return messages

    def _sliding_window(self, messages: list[dict], max_tokens: int) -> list[dict]:
        """
        从最新消息开始倒序保留，估算 token 数（4 字符≈1 token），
        直到接近 max_tokens 的 compression_threshold。
        始终保留第一条（通常是 user 的初始请求）。
        """
        threshold = self.strategy.get("compression_threshold", 0.8)
        budget = int(max_tokens * threshold)

        if not messages:
            return messages

        selected = []
        used = 0
        for msg in reversed(messages):
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
            tokens = len(str(content)) // 4
            if used + tokens > budget and selected:
                break
            selected.append(msg)
            used += tokens

        result = list(reversed(selected))
        # 确保第一条保留（context anchor）
        if messages and messages[0] not in result:
            result = [messages[0]] + result
        return result

    def _summary_window(self, messages: list[dict], max_tokens: int) -> list[dict]:
        """
        当历史超出阈值时，把早期消息替换为摘要占位符。
        摘要由 LLM 在运行时生成（此处为 placeholder，实际调用需传入 llm 实例）。
        """
        threshold = self.strategy.get("compression_threshold", 0.8)
        budget = int(max_tokens * threshold)

        total_chars = sum(len(str(m.get("content", ""))) for m in messages)
        if total_chars // 4 <= budget:
            return messages

        # 保留最近 N 条，把早期内容标记为已摘要
        keep_recent = max(4, len(messages) // 2)
        old = messages[:-keep_recent]
        recent = messages[-keep_recent:]

        if old:
            summary_text = f"[Earlier conversation summary — {len(old)} messages omitted for context length]"
            summary_msg = {"role": "assistant", "content": summary_text}
            return [messages[0], summary_msg] + recent

        return messages
