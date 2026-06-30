from sqlalchemy import Column, String, Integer, Float, Date, DateTime, func
from app.database import Base


class UsageStat(Base):
    __tablename__ = "usage_stats"

    id = Column(String(36), primary_key=True)
    agent_id = Column(String(36), nullable=False, index=True)
    user_id = Column(String(36), nullable=False, index=True)
    stat_date = Column(Date, nullable=False, index=True)
    session_count = Column(Integer, default=0)
    runner_count = Column(Integer, default=0)
    task_count = Column(Integer, default=0)
    tool_call_count = Column(Integer, default=0)
    input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    total_cost_usd = Column(Float, default=0.0)
    avg_latency_ms = Column(Float, default=0.0)
    error_count = Column(Integer, default=0)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class ChannelConfig(Base):
    __tablename__ = "channel_configs"

    id = Column(String(36), primary_key=True)
    agent_id = Column(String(36), nullable=False, index=True)
    channel = Column(String(50), nullable=False)  # feishu | wechat | telegram | webhook
    config_json = Column(String(4096), nullable=False, default='{}')
    is_active = Column(Integer, default=1)
    created_at = Column(DateTime, server_default=func.now())
