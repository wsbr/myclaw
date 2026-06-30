"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-01-01 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── users ──────────────────────────────────────────────────────────
    op.create_table('users',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('email', sa.String(255), nullable=False, unique=True),
        sa.Column('username', sa.String(100), nullable=False, unique=True),
        sa.Column('password_hash', sa.String(255), nullable=False),
        sa.Column('role', sa.String(20), nullable=False, server_default='user'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='1'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()')),
    )
    op.create_index('ix_users_email', 'users', ['email'])

    # ── api_tokens ─────────────────────────────────────────────────────
    op.create_table('api_tokens',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('agent_id', sa.String(36), nullable=True),   # FK added after agents table
        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('token_hash', sa.String(255), nullable=False, unique=True),
        sa.Column('last_used_at', sa.DateTime()),
        sa.Column('expires_at', sa.DateTime()),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()')),
    )
    op.create_index('ix_api_tokens_user_id', 'api_tokens', ['user_id'])

    # ── tools ──────────────────────────────────────────────────────────
    op.create_table('tools',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('name', sa.String(100), nullable=False, unique=True),
        sa.Column('display_name', sa.String(200), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('input_schema', sa.JSON(), nullable=False),
        sa.Column('output_schema', sa.JSON()),
        sa.Column('is_builtin', sa.Boolean(), server_default='0'),
        sa.Column('implementation_key', sa.String(200)),
        sa.Column('version', sa.String(20), server_default='1.0.0'),
        sa.Column('is_active', sa.Boolean(), server_default='1'),
        sa.Column('created_by', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()')),
    )

    # ── skills ─────────────────────────────────────────────────────────
    op.create_table('skills',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('name', sa.String(100), nullable=False, unique=True),
        sa.Column('display_name', sa.String(200), nullable=False),
        sa.Column('description', sa.String(500), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('version', sa.String(20), server_default='1.0.0'),
        sa.Column('is_active', sa.Boolean(), server_default='1'),
        sa.Column('created_by', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()')),
    )

    # ── mcp_servers ────────────────────────────────────────────────────
    op.create_table('mcp_servers',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('display_name', sa.String(200), nullable=False),
        sa.Column('description', sa.Text()),
        sa.Column('endpoint', sa.String(500), nullable=False),
        sa.Column('auth_type', sa.String(20), server_default='none'),
        sa.Column('auth_config_encrypted', sa.JSON()),
        sa.Column('health_check_url', sa.String(500)),
        sa.Column('capability_summary', sa.Text()),
        sa.Column('is_active', sa.Boolean(), server_default='1'),
        sa.Column('created_by', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()')),
    )

    # ── model_providers ────────────────────────────────────────────────
    op.create_table('model_providers',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('provider', sa.String(50), nullable=False),
        sa.Column('base_url', sa.String(500)),
        sa.Column('api_key_encrypted', sa.String(1000)),
        sa.Column('is_active', sa.Boolean(), server_default='1'),
        sa.Column('created_by', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()')),
    )

    # ── available_models ───────────────────────────────────────────────
    op.create_table('available_models',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('provider_id', sa.String(36), sa.ForeignKey('model_providers.id', ondelete='CASCADE'), nullable=False),
        sa.Column('model_id', sa.String(200), nullable=False),
        sa.Column('display_name', sa.String(200), nullable=False),
        sa.Column('max_tokens', sa.Integer(), server_default='8192'),
        sa.Column('supports_tools', sa.Boolean(), server_default='1'),
        sa.Column('supports_streaming', sa.Boolean(), server_default='1'),
        sa.Column('is_active', sa.Boolean(), server_default='1'),
    )
    op.create_index('ix_available_models_provider_id', 'available_models', ['provider_id'])

    # ── agents ─────────────────────────────────────────────────────────
    op.create_table('agents',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('owner_id', sa.String(36), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('name', sa.String(200), nullable=False),
        sa.Column('description', sa.Text()),
        sa.Column('avatar_url', sa.String(500)),
        sa.Column('status', sa.String(20), server_default='draft'),
        sa.Column('version', sa.Integer(), server_default='1'),
        sa.Column('modes', sa.JSON()),
        sa.Column('model_config', sa.JSON()),        # mapped as model_config_ in ORM
        sa.Column('system_prompt_blocks', sa.JSON()),
        sa.Column('context_strategy', sa.JSON()),
        sa.Column('memory_config', sa.JSON()),
        sa.Column('sandbox_config', sa.JSON()),
        sa.Column('supported_channels', sa.JSON()),
        sa.Column('group_config', sa.JSON()),
        sa.Column('alert_config', sa.JSON()),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()')),
    )
    op.create_index('ix_agents_owner_id', 'agents', ['owner_id'])

    # FK from api_tokens to agents (now that agents table exists)
    op.create_foreign_key('fk_api_tokens_agent_id', 'api_tokens', 'agents', ['agent_id'], ['id'], ondelete='CASCADE')

    # ── agent_versions ─────────────────────────────────────────────────
    op.create_table('agent_versions',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('agent_id', sa.String(36), sa.ForeignKey('agents.id', ondelete='CASCADE'), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('snapshot', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()')),
    )
    op.create_index('ix_agent_versions_agent_id', 'agent_versions', ['agent_id'])

    # ── agent_tools (composite PK) ─────────────────────────────────────
    op.create_table('agent_tools',
        sa.Column('agent_id', sa.String(36), sa.ForeignKey('agents.id', ondelete='CASCADE'), nullable=False),
        sa.Column('tool_id', sa.String(36), sa.ForeignKey('tools.id', ondelete='CASCADE'), nullable=False),
        sa.Column('config', sa.JSON()),
        sa.Column('sort_order', sa.Integer(), server_default='0'),
        sa.PrimaryKeyConstraint('agent_id', 'tool_id'),
    )

    # ── agent_skills (composite PK) ────────────────────────────────────
    op.create_table('agent_skills',
        sa.Column('agent_id', sa.String(36), sa.ForeignKey('agents.id', ondelete='CASCADE'), nullable=False),
        sa.Column('skill_id', sa.String(36), sa.ForeignKey('skills.id', ondelete='CASCADE'), nullable=False),
        sa.Column('sort_order', sa.Integer(), server_default='0'),
        sa.PrimaryKeyConstraint('agent_id', 'skill_id'),
    )

    # ── agent_mcp_servers (composite PK) ───────────────────────────────
    op.create_table('agent_mcp_servers',
        sa.Column('agent_id', sa.String(36), sa.ForeignKey('agents.id', ondelete='CASCADE'), nullable=False),
        sa.Column('mcp_server_id', sa.String(36), sa.ForeignKey('mcp_servers.id', ondelete='CASCADE'), nullable=False),
        sa.Column('sort_order', sa.Integer(), server_default='0'),
        sa.PrimaryKeyConstraint('agent_id', 'mcp_server_id'),
    )

    # ── agent_builtin_tools (composite PK) ─────────────────────────────
    op.create_table('agent_builtin_tools',
        sa.Column('agent_id', sa.String(36), sa.ForeignKey('agents.id', ondelete='CASCADE'), nullable=False),
        sa.Column('tool_key', sa.String(100), nullable=False),
        sa.Column('enabled', sa.Boolean(), server_default='1'),
        sa.PrimaryKeyConstraint('agent_id', 'tool_key'),
    )

    # ── sessions ───────────────────────────────────────────────────────
    op.create_table('sessions',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('agent_id', sa.String(36), sa.ForeignKey('agents.id'), nullable=False),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('mode', sa.String(20), nullable=False, server_default='chat'),
        sa.Column('title', sa.String(500)),
        sa.Column('status', sa.String(20), server_default='active'),
        sa.Column('fork_from_session_id', sa.String(36)),
        sa.Column('fork_from_runner_id', sa.String(36)),
        sa.Column('fork_from_span_id', sa.String(36)),
        sa.Column('metadata', sa.JSON()),              # mapped as metadata_ in ORM
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()')),
    )
    op.create_index('ix_sessions_agent_id', 'sessions', ['agent_id'])
    op.create_index('ix_sessions_user_id', 'sessions', ['user_id'])

    # ── runners ────────────────────────────────────────────────────────
    op.create_table('runners',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('session_id', sa.String(36), sa.ForeignKey('sessions.id', ondelete='CASCADE'), nullable=False),
        sa.Column('agent_id', sa.String(36), sa.ForeignKey('agents.id'), nullable=False),
        sa.Column('agent_version', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('parent_runner_id', sa.String(36)),
        sa.Column('status', sa.String(20), server_default='pending'),
        sa.Column('input', sa.JSON()),                 # mapped as input_data in ORM
        sa.Column('output', sa.JSON()),                # mapped as output_data in ORM
        sa.Column('error', sa.Text()),
        sa.Column('total_tokens', sa.Integer(), server_default='0'),
        sa.Column('total_cost_usd', sa.Float(), server_default='0.0'),
        sa.Column('duration_ms', sa.Integer()),
        sa.Column('started_at', sa.DateTime()),
        sa.Column('completed_at', sa.DateTime()),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()')),
    )
    op.create_index('ix_runners_session_id', 'runners', ['session_id'])

    # ── spans ──────────────────────────────────────────────────────────
    op.create_table('spans',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('runner_id', sa.String(36), sa.ForeignKey('runners.id', ondelete='CASCADE'), nullable=False),
        sa.Column('session_id', sa.String(36), sa.ForeignKey('sessions.id', ondelete='CASCADE'), nullable=False),
        sa.Column('parent_span_id', sa.String(36)),
        sa.Column('span_type', sa.String(50), nullable=False),
        sa.Column('name', sa.String(200), nullable=False),
        sa.Column('status', sa.String(20), server_default='running'),
        sa.Column('snapshot', sa.LargeBinary()),
        sa.Column('request', sa.LargeBinary()),
        sa.Column('response', sa.LargeBinary()),
        sa.Column('input_tokens', sa.Integer(), server_default='0'),
        sa.Column('output_tokens', sa.Integer(), server_default='0'),
        sa.Column('cost_usd', sa.Float(), server_default='0.0'),
        sa.Column('duration_ms', sa.Integer()),
        sa.Column('error', sa.Text()),
        sa.Column('started_at', sa.DateTime(), server_default=sa.text('now()')),
        sa.Column('completed_at', sa.DateTime()),
    )
    op.create_index('ix_spans_runner_id', 'spans', ['runner_id'])
    op.create_index('ix_spans_session_id', 'spans', ['session_id'])

    # ── orchestration_graphs ───────────────────────────────────────────
    op.create_table('orchestration_graphs',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('session_id', sa.String(36), sa.ForeignKey('sessions.id', ondelete='CASCADE'), nullable=False, unique=True),
        sa.Column('goal', sa.Text(), nullable=False),
        sa.Column('graph_json', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(20), server_default='draft'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()')),
    )

    # ── tasks ──────────────────────────────────────────────────────────
    op.create_table('tasks',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('agent_id', sa.String(36), sa.ForeignKey('agents.id'), nullable=False),
        sa.Column('agent_version', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('session_id', sa.String(36), sa.ForeignKey('sessions.id')),
        sa.Column('parent_task_id', sa.String(36)),
        sa.Column('title', sa.String(500)),
        sa.Column('status', sa.String(30), nullable=False, server_default='pending'),
        sa.Column('waiting_reason', sa.String(500)),
        sa.Column('trigger_type', sa.String(30), nullable=False, server_default='immediate'),
        sa.Column('cron_expr', sa.String(100)),
        sa.Column('event_type', sa.String(200)),
        sa.Column('scheduled_at', sa.DateTime()),
        sa.Column('input', sa.JSON()),                 # mapped as input_data in ORM
        sa.Column('output', sa.JSON()),                # mapped as output_data in ORM
        sa.Column('output_channel', sa.String(50), nullable=False, server_default='platform_ui'),
        sa.Column('celery_task_id', sa.String(200)),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()')),
        sa.Column('started_at', sa.DateTime()),
        sa.Column('completed_at', sa.DateTime()),
    )
    op.create_index('ix_tasks_agent_id', 'tasks', ['agent_id'])
    op.create_index('ix_tasks_user_id', 'tasks', ['user_id'])
    op.create_index('ix_tasks_status', 'tasks', ['status'])

    # ── memories ───────────────────────────────────────────────────────
    op.create_table('memories',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('agent_id', sa.String(36), nullable=False),
        sa.Column('user_id', sa.String(36), nullable=False),
        sa.Column('session_id', sa.String(36)),
        sa.Column('memory_type', sa.String(20), nullable=False),
        sa.Column('scope', sa.String(20), nullable=False),
        sa.Column('key', sa.String(255), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('embedding', sa.JSON()),
        sa.Column('relevance_score', sa.Float()),
        sa.Column('access_count', sa.Integer(), server_default='0'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()')),
        sa.Column('expires_at', sa.DateTime()),
    )
    op.create_index('ix_memories_agent_id', 'memories', ['agent_id'])
    op.create_index('ix_memories_user_id', 'memories', ['user_id'])
    op.create_index('ix_memories_key', 'memories', ['key'])

    # ── usage_stats ────────────────────────────────────────────────────
    op.create_table('usage_stats',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('agent_id', sa.String(36), nullable=False),
        sa.Column('user_id', sa.String(36), nullable=False),
        sa.Column('stat_date', sa.Date(), nullable=False),
        sa.Column('session_count', sa.Integer(), server_default='0'),
        sa.Column('runner_count', sa.Integer(), server_default='0'),
        sa.Column('task_count', sa.Integer(), server_default='0'),
        sa.Column('tool_call_count', sa.Integer(), server_default='0'),
        sa.Column('input_tokens', sa.Integer(), server_default='0'),
        sa.Column('output_tokens', sa.Integer(), server_default='0'),
        sa.Column('total_cost_usd', sa.Float(), server_default='0.0'),
        sa.Column('avg_latency_ms', sa.Float(), server_default='0.0'),
        sa.Column('error_count', sa.Integer(), server_default='0'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()')),
    )
    op.create_index('ix_usage_stats_agent_id', 'usage_stats', ['agent_id'])
    op.create_index('ix_usage_stats_date', 'usage_stats', ['stat_date'])

    # ── channel_configs ────────────────────────────────────────────────
    op.create_table('channel_configs',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('agent_id', sa.String(36), nullable=False),
        sa.Column('channel', sa.String(50), nullable=False),
        sa.Column('config_json', sa.String(4096), nullable=False, server_default='{}'),
        sa.Column('is_active', sa.Integer(), server_default='1'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()')),
    )
    op.create_index('ix_channel_configs_agent_id', 'channel_configs', ['agent_id'])


def downgrade() -> None:
    tables = [
        'channel_configs', 'usage_stats', 'memories', 'tasks',
        'orchestration_graphs', 'spans', 'runners', 'sessions',
        'agent_builtin_tools', 'agent_mcp_servers', 'agent_skills', 'agent_tools',
        'agent_versions', 'agents', 'available_models', 'model_providers',
        'mcp_servers', 'skills', 'tools', 'api_tokens', 'users',
    ]
    for t in tables:
        op.drop_table(t)
