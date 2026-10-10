"""Human-facing localized names for platform tool registry entries."""

from __future__ import annotations

import re


BUILTIN_TOOL_DISPLAY_NAMES_ZH: dict[str, str] = {
    "apply_patch": "应用补丁",
    "ask_user": "询问用户",
    "browser_interact": "操作浏览器",
    "browser_navigate": "浏览器导航",
    "browser_observe": "观察浏览器页面",
    "browser_screenshot": "浏览器截图",
    "delete_path": "删除路径",
    "execute": "执行命令",
    "discover_agents": "发现智能体",
    "find_agent_user": "查找可联系用户",
    "invite_agent": "邀请智能体",
    "invoke_agent": "调用智能体",
    "knowledge_search": "搜索知识库",
    "list_files": "列出文件",
    "memory_propose": "提议记忆",
    "memory_search": "搜索记忆",
    "publish_artifact": "发布产物",
    "read_file": "读取文件",
    "search_files": "搜索文件",
    "search_group_history": "搜索群组历史",
    "search_tools": "搜索工具",
    "select_group_members": "选择群组成员",
    "send_group_message": "发送群组消息",
    "schedule_task": "定时任务",
    "web_fetch": "获取网页",
    "web_search": "搜索网页",
    "write_file": "写入文件",
    "update_plan": "更新计划",
    "update_shared_task": "更新共享任务",
}


MCP_TOOL_DISPLAY_NAMES_ZH: dict[str, str] = {
    "fetch_openai_doc": "获取 OpenAI 文档",
    "get_openapi_spec": "获取 OpenAPI 规范",
    "list_api_endpoints": "列出 API 接口",
    "list_openai_docs": "列出 OpenAI 文档",
    "search_openai_docs": "搜索 OpenAI 文档",
}


_CJK_PATTERN = re.compile(r"[\u3400-\u9fff]")


def builtin_tool_display_name_zh(tool_key: str) -> str:
    """Require every registered built-in Tool to have an intentional Chinese name."""

    return BUILTIN_TOOL_DISPLAY_NAMES_ZH[tool_key]


def mcp_tool_display_name_zh(
    remote_name: str,
    remote_title: str,
    *,
    current_display_name: str = "",
    current_display_name_zh: str = "",
) -> str:
    """Resolve MCP localization without overwriting an administrator translation."""

    title = remote_title.strip()
    if _CJK_PATTERN.search(title):
        return title
    current_zh = current_display_name_zh.strip()
    if current_zh and current_zh != current_display_name.strip() and _CJK_PATTERN.search(current_zh):
        return current_zh
    return MCP_TOOL_DISPLAY_NAMES_ZH.get(remote_name, "")
