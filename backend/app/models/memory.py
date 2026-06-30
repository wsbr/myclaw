from sqlalchemy import Column, String, Text, Float, Integer, DateTime, JSON, func
from app.database import Base


class Memory(Base):
    __tablename__ = "memories"

    id = Column(String(36), primary_key=True)
    agent_id = Column(String(36), nullable=False, index=True)
    user_id = Column(String(36), nullable=False, index=True)
    session_id = Column(String(36), index=True)
    memory_type = Column(String(20), nullable=False)  # 'short_term' | 'long_term'
    scope = Column(String(20), nullable=False)         # 'session' | 'user' | 'global'
    key = Column(String(255), nullable=False, index=True)
    content = Column(Text, nullable=False)
    embedding = Column(JSON)
    relevance_score = Column(Float)
    access_count = Column(Integer, default=0)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    expires_at = Column(DateTime)
