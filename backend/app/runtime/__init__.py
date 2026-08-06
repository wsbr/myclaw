"""Agent-native v2 unified runtime."""

from app.runtime.action_dispatcher import ActionDispatcher, DispatchResult
from app.runtime.agent_loop_coordinator import AgentLoopCoordinator, AgentLoopResult, LoopIdentity
from app.runtime.context_assembler import ContextAssembler, ContextPackage
from app.runtime.model_adapter import ModelAdapter
from app.runtime.snapshot_resolver import SnapshotResolver

__all__ = [
    "ActionDispatcher", "DispatchResult", "AgentLoopCoordinator", "AgentLoopResult",
    "LoopIdentity", "ContextAssembler", "ContextPackage", "ModelAdapter", "SnapshotResolver",
]
