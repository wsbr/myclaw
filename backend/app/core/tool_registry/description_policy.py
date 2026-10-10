"""Runtime presentation fixes for historical built-in Tool descriptions.

Frozen ToolVersions remain untouched. Only known platform-authored phrases
are projected; arbitrary MCP and administrator contracts are not rewritten.
"""

from __future__ import annotations


REPLACEMENTS = {
    "app.core.tool_registry.builtin.read_file:ReadFileTool": (
        ("use list_files/search_files to discover paths first", "do not guess unknown paths; use an available path-discovery capability when needed"),
    ),
    "app.core.tool_registry.builtin.workspace_catalog:ListFilesTool": (
        ("do not use for file contents; use read_file or search_files", "do not use for file contents; reading or searching contents requires its own available capability"),
    ),
    "app.core.tool_registry.builtin.workspace_catalog:ApplyPatchTool": (
        ("use write_file for whole-file create/replace", "whole-file creation or replacement requires its own available capability"),
    ),
    "app.core.tool_registry.builtin.web_tools:WebSearchTool": (
        ("use web_fetch/read_file instead", "use a separately available page or file reading capability when appropriate"),
        ("use web_fetch/read_file", "use a separately available page or file reading capability when appropriate"),
    ),
    "app.core.tool_registry.builtin.web_tools:WebFetchTool": (
        ("use web_search, Browser Tools, or read_file", "those tasks require their own available discovery, interaction, or file capabilities"),
    ),
    "app.core.tool_registry.builtin.browser_tools:BrowserNavigateTool": (
        ("use web_fetch for non-interactive evidence", "prefer a separately available page-reading capability for non-interactive evidence"),
    ),
}


def project_tool_description(definition: dict) -> str:
    description = str(definition.get("description") or "")
    for old, new in REPLACEMENTS.get(definition.get("implementation_ref"), ()):
        description = description.replace(old, new)
    return description
