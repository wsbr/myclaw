"""execution plans

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-08 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'execution_plans',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('session_id', sa.String(36), sa.ForeignKey('sessions.id', ondelete='CASCADE'), nullable=False),
        sa.Column('goal', sa.Text(), nullable=False),
        sa.Column('plan_json', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(20), server_default='draft'),
        sa.Column('task_id', sa.String(36), sa.ForeignKey('tasks.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()')),
    )
    op.create_index('ix_execution_plans_session_id', 'execution_plans', ['session_id'])


def downgrade() -> None:
    op.drop_index('ix_execution_plans_session_id', 'execution_plans')
    op.drop_table('execution_plans')
