"""Normalize Chinese display names in the platform Tool registry.

Revision ID: 0043
Revises: 0042
"""

from alembic import op
import sqlalchemy as sa


revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


MCP_TOOL_DISPLAY_NAMES_ZH = {
    "fetch_openai_doc": "获取 OpenAI 文档",
    "get_openapi_spec": "获取 OpenAPI 规范",
    "list_api_endpoints": "列出 API 接口",
    "list_openai_docs": "列出 OpenAI 文档",
    "search_openai_docs": "搜索 OpenAI 文档",
}


def upgrade() -> None:
    bind = op.get_bind()
    for remote_name, display_name_zh in MCP_TOOL_DISPLAY_NAMES_ZH.items():
        bind.execute(
            sa.text(
                "UPDATE platform_tool SET display_name_zh = :display_name_zh "
                "WHERE origin = 'mcp' AND tool_key LIKE :tool_key_suffix "
                "AND display_name_zh = display_name"
            ),
            {
                "display_name_zh": display_name_zh,
                "tool_key_suffix": f"%__{remote_name}",
            },
        )
    op.execute(sa.text(
        "UPDATE platform_tool SET display_name_zh = '' "
        "WHERE origin = 'mcp' AND display_name_zh = display_name"
    ))


def downgrade() -> None:
    # Unknown MCP titles intentionally remain blank: the previous copied-English
    # value cannot be distinguished from an administrator-authored value safely.
    pass
