import uuid
from datetime import datetime
from sqlalchemy import String, Boolean, DateTime, Integer, Text, JSON, ForeignKey, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(SAEnum("draft", "active", "inactive"), nullable=False, default="draft")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    modes: Mapped[dict] = mapped_column(JSON, nullable=False, default=lambda: ["chat"])
    model_config_: Mapped[dict] = mapped_column("model_config", JSON, nullable=False, default=dict)
    system_prompt_blocks: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    context_strategy: Mapped[dict] = mapped_column(JSON, nullable=False, default=lambda: {"window_type": "sliding", "max_tokens": 128000})
    memory_config: Mapped[dict] = mapped_column(JSON, nullable=False, default=lambda: {"short_term": True, "long_term": {"enabled": False}})
    sandbox_config: Mapped[dict] = mapped_column(JSON, nullable=False, default=lambda: {"enabled": False})
    supported_channels: Mapped[list] = mapped_column(JSON, nullable=False, default=lambda: ["platform_ui"])
    group_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    alert_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    owner: Mapped["User"] = relationship("User", back_populates="agents")
    versions: Mapped[list["AgentVersion"]] = relationship("AgentVersion", back_populates="agent", lazy="select")
    agent_tools: Mapped[list["AgentTool"]] = relationship("AgentTool", back_populates="agent", lazy="select")
    agent_skills: Mapped[list["AgentSkill"]] = relationship("AgentSkill", back_populates="agent", lazy="select")
    agent_mcp_servers: Mapped[list["AgentMcpServer"]] = relationship("AgentMcpServer", back_populates="agent", lazy="select")
    agent_builtin_tools: Mapped[list["AgentBuiltinTool"]] = relationship("AgentBuiltinTool", back_populates="agent", lazy="select")


class AgentVersion(Base):
    __tablename__ = "agent_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    agent: Mapped["Agent"] = relationship("Agent", back_populates="versions")


class AgentTool(Base):
    __tablename__ = "agent_tools"

    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True)
    tool_id: Mapped[str] = mapped_column(String(36), ForeignKey("tools.id", ondelete="CASCADE"), primary_key=True)
    config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    agent: Mapped["Agent"] = relationship("Agent", back_populates="agent_tools")
    tool: Mapped["Tool"] = relationship("Tool")


class AgentSkill(Base):
    __tablename__ = "agent_skills"

    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True)
    skill_id: Mapped[str] = mapped_column(String(36), ForeignKey("skills.id", ondelete="CASCADE"), primary_key=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    agent: Mapped["Agent"] = relationship("Agent", back_populates="agent_skills")
    skill: Mapped["Skill"] = relationship("Skill")


class AgentMcpServer(Base):
    __tablename__ = "agent_mcp_servers"

    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True)
    mcp_server_id: Mapped[str] = mapped_column(String(36), ForeignKey("mcp_servers.id", ondelete="CASCADE"), primary_key=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    agent: Mapped["Agent"] = relationship("Agent", back_populates="agent_mcp_servers")
    mcp_server: Mapped["McpServer"] = relationship("McpServer")


class AgentBuiltinTool(Base):
    __tablename__ = "agent_builtin_tools"

    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True)
    tool_key: Mapped[str] = mapped_column(String(100), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    agent: Mapped["Agent"] = relationship("Agent", back_populates="agent_builtin_tools")
