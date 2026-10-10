"""Model-facing Browser Tools backed by the isolated Chromium Worker."""

from __future__ import annotations

from pathlib import Path

from app.core.tool_registry.base import BaseTool, RuntimeContext, ToolResult
from app.models.common import uuid7
from app.services.artifact_service import publish_artifact
from app.services.browser_worker_service import (
    BrowserOutcomeUnknown, BrowserWorkerError, browser_manager,
)
from app.services.workspace_file_service import (
    resolve_read_path, resolve_workspace_path, sha256_file,
)


def _failure(error: BrowserWorkerError) -> ToolResult:
    outcome_unknown = isinstance(error, BrowserOutcomeUnknown)
    status = "outcome_unknown" if outcome_unknown else "failed"
    return ToolResult(
        status=status, summary=str(error), error=str(error), error_code=error.code,
        error_category=error.category, retryable=False,
        # Revision, fingerprint, validation, and authorization failures happen
        # before Playwright is allowed to touch the page. This proof prevents
        # the generic non-idempotent settler from inflating a safe conflict to
        # outcome_unknown. BrowserOutcomeUnknown intentionally carries no such
        # proof and must never be replayed automatically.
        error_details={} if outcome_unknown else {"side_effect_started": False},
    )


class BrowserNavigateTool(BaseTool):
    name = "browser_navigate"
    description = (
        "WHEN: open a public URL in this MyClaw Session's isolated Chromium. "
        "WHEN NOT: prefer a separately available page-reading capability for non-interactive evidence; Browser never inherits user cookies/profile. "
        "RETURNS: final URL, origin, title, HTTP status, and page revision. "
        "RECOVERY: inspect navigation errors or choose a public URL, then observe."
    )
    input_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
        "properties": {
            "url": {"type": "string", "format": "uri", "maxLength": 2000},
            "wait_until": {"type": "string", "enum": ["commit", "domcontentloaded", "load"], "default": "domcontentloaded"},
        },
        "required": ["url"], "additionalProperties": False,
    }

    async def execute(self, args, context, db=None):
        try:
            data = await browser_manager.navigate(
                db, session_id=context.session_id, workspace_id=context.workspace_id,
                url=args["url"], wait_until=args.get("wait_until", "domcontentloaded"),
            )
        except BrowserWorkerError as error:
            return _failure(error)
        return ToolResult(status="succeeded", summary=f"Navigated isolated Chromium to {data['url']}", data=data,
                          content_blocks=[{"type": "json", "value": data}], provider={"name": "isolated-chromium"},
                          evidence=[{"kind": "browser_page", **data}])


class BrowserObserveTool(BaseTool):
    name = "browser_observe"
    description = (
        "WHEN: inspect the current isolated page before any interaction. "
        "WHEN NOT: never guess CSS/XPath selectors. "
        "RETURNS: accessibility snapshot, stable element refs, DOM fingerprint, origin, and new page revision. "
        "RECOVERY: observe again after any stale revision or DOM conflict."
    )
    input_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
        "properties": {"max_elements": {"type": "integer", "minimum": 1, "maximum": 500, "default": 200}},
        "additionalProperties": False,
    }

    async def execute(self, args, context, db=None):
        try:
            data = await browser_manager.observe(
                db, session_id=context.session_id, workspace_id=context.workspace_id,
                max_elements=int(args.get("max_elements", 200)),
            )
        except BrowserWorkerError as error:
            return _failure(error)
        return ToolResult(status="succeeded", summary=f"Observed {len(data['elements'])} interactive elements", data=data,
                          content_blocks=[{"type": "json", "value": data}], provider={"name": "isolated-chromium"},
                          evidence=[{"kind": "browser_observation", "url": data["url"], "page_revision": data["page_revision"], "dom_fingerprint": data["dom_fingerprint"]}])


class BrowserInteractTool(BaseTool):
    name = "browser_interact"
    description = (
        "WHEN: interact only with an element_ref from the latest browser_observe revision. "
        "WHEN NOT: never guess selectors or replay outcome_unknown actions; sensitive operations require exact approval. "
        "RETURNS: action, resulting URL/origin/revision, download Artifact when applicable. "
        "RECOVERY: observe again on revision/DOM conflict; ask the user after outcome_unknown."
    )
    input_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["click", "input", "select", "press", "scroll", "upload", "download"]},
            "element_ref": {"type": "string", "pattern": "^e[1-9][0-9]*$"},
            "page_revision": {"type": "integer", "minimum": 1},
            "dom_fingerprint": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "text": {"type": "string", "maxLength": 20000},
            "value": {"type": "string", "maxLength": 2000},
            "key": {"type": "string", "maxLength": 100},
            "delta_x": {"type": "integer", "minimum": -10000, "maximum": 10000},
            "delta_y": {"type": "integer", "minimum": -10000, "maximum": 10000},
            "upload_path": {"type": "string"},
            "download_name": {"type": "string", "pattern": "^[^/\\\\\\x00-\\x1f]{1,200}$"},
        },
        "required": ["action", "page_revision", "dom_fingerprint"],
        "allOf": [
            {"if": {"properties": {"action": {"enum": ["click", "input", "select", "upload", "download"]}}}, "then": {"required": ["element_ref"]}},
            {"if": {"properties": {"action": {"const": "input"}}}, "then": {"required": ["text"]}},
            {"if": {"properties": {"action": {"const": "select"}}}, "then": {"required": ["value"]}},
            {"if": {"properties": {"action": {"const": "press"}}}, "then": {"required": ["key"]}},
            {"if": {"properties": {"action": {"const": "upload"}}}, "then": {"required": ["upload_path"]}},
        ],
        "additionalProperties": False,
    }

    async def execute(self, args, context, db=None):
        upload = None
        if args["action"] == "upload":
            upload, _ = await resolve_read_path(db, context=context, value=args["upload_path"])
            if not upload.is_file() or upload.is_symlink():
                return ToolResult(status="failed", summary="Upload file is unavailable", error="Upload file is unavailable", error_code="browser_upload_missing", error_category="validation")
        download = None
        relative_download = None
        if args["action"] == "download":
            name = args.get("download_name") or f"download-{uuid7()}"
            _, download, relative_download = await resolve_workspace_path(
                db, workspace_id=context.workspace_id, session_id=context.session_id,
                relative_path=f"browser/downloads/{name}", agent_id=context.agent_id,
                group_session_id=context.group_session_id,
            )
            if download.exists():
                return ToolResult(
                    status="failed", summary="Download target already exists",
                    error="Download target already exists",
                    error_code="browser_download_conflict", error_category="conflict",
                    retryable=True,
                )
            download.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = await browser_manager.interact(
                db, session_id=context.session_id, workspace_id=context.workspace_id,
                args=args, upload_path=upload, download_path=download,
            )
        except BrowserWorkerError as error:
            return _failure(error)
        artifacts = []
        if download and download.is_file():
            try:
                artifact = await publish_artifact(
                    db, context=context,
                    args={"path": relative_download, "name": download.name},
                )
            except Exception:
                # The metadata transaction rolls back together with the Tool
                # invocation. Remove the matching browser output as well so an
                # exact retry does not conflict with an orphaned file.
                download.unlink(missing_ok=True)
                raise
            artifacts.append(artifact.id)
            data["download"] = {
                **(data.get("download") or {}), "path": relative_download,
                "artifact_id": artifact.id, "checksum": artifact.checksum,
            }
        return ToolResult(status="succeeded", summary=f"Browser action {args['action']} completed", data=data,
                          content_blocks=[{"type": "json", "value": data}] + ([{"type": "artifact_reference", "artifact_id": artifacts[0]}] if artifacts else []),
                          artifact_ids=artifacts, provider={"name": "isolated-chromium"},
                          evidence=[{"kind": "browser_interaction", **data}])


class BrowserScreenshotTool(BaseTool):
    name = "browser_screenshot"
    description = (
        "WHEN: capture immutable visual evidence from the current isolated Browser page. "
        "WHEN NOT: do not treat pixels alone as proof of a completed external side effect. "
        "RETURNS: Workspace path plus published Artifact ID/checksum and page revision. "
        "RECOVERY: navigate/observe again if the Browser page is unavailable."
    )
    input_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
        "properties": {
            "name": {"type": "string", "pattern": "^[^/\\\\\\x00-\\x1f]{1,180}\\.png$"},
            "full_page": {"type": "boolean", "default": True},
        },
        "required": ["name"], "additionalProperties": False,
    }

    async def execute(self, args, context, db=None):
        _, target, relative = await resolve_workspace_path(
            db, workspace_id=context.workspace_id, session_id=context.session_id,
            relative_path=f"browser/screenshots/{args['name']}",
            agent_id=context.agent_id, group_session_id=context.group_session_id,
        )
        if target.exists():
            return ToolResult(
                status="failed", summary="Screenshot target already exists",
                error="Screenshot target already exists",
                error_code="browser_screenshot_conflict", error_category="conflict",
                retryable=True,
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = await browser_manager.screenshot(
                db, session_id=context.session_id, workspace_id=context.workspace_id,
                path=target, full_page=bool(args.get("full_page", True)),
            )
        except BrowserWorkerError as error:
            return _failure(error)
        try:
            artifact = await publish_artifact(
                db, context=context,
                args={"path": relative, "name": target.name, "mime_type": "image/png"},
            )
        except Exception:
            # The database transaction will roll back, so remove the matching
            # filesystem capture as well. Otherwise a transient publication
            # failure leaves an orphan that makes every exact retry conflict.
            target.unlink(missing_ok=True)
            raise
        data.update({"path": relative, "artifact_id": artifact.id, "checksum": sha256_file(target)})
        return ToolResult(status="succeeded", summary="Browser screenshot captured and published", data=data,
                          content_blocks=[{"type": "image", "artifact_id": artifact.id, "mime_type": "image/png"}],
                          artifact_ids=[artifact.id], provider={"name": "isolated-chromium"},
                          evidence=[{"kind": "browser_screenshot", **data}])
