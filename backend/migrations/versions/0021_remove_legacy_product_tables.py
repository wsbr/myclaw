"""remove the pre-phase-one product schema

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-02 08:00:00.000000

The project has no production data to preserve. Revisions 0001-0003 remain as
historical install steps, while this revision removes their obsolete runtime
tables after the phase-one replacement schema is complete.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic import op


revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


LEGACY_TABLES_IN_DROP_ORDER = (
    "notifications",
    "execution_plans",
    "channel_configs",
    "usage_stats",
    "memories",
    "tasks",
    "orchestration_graphs",
    "spans",
    "runners",
    "sessions",
    "agent_builtin_tools",
    "agent_mcp_servers",
    "agent_skills",
    "agent_tools",
    "agent_versions",
    "api_tokens",
    "agents",
    "available_models",
    "model_providers",
    "mcp_servers",
    "skills",
    "tools",
    "users",
)


def upgrade() -> None:
    for table_name in LEGACY_TABLES_IN_DROP_ORDER:
        op.drop_table(table_name)


def _legacy_upgrade(filename: str) -> None:
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(f"myclaw_legacy_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load historical migration {filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.upgrade()


def downgrade() -> None:
    _legacy_upgrade("0001_initial_schema.py")
    _legacy_upgrade("0002_execution_plans.py")
    _legacy_upgrade("0003_notifications.py")
