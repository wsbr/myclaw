"""Per-Session isolated Chromium control plane used by Browser Tools."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import Browser, BrowserContext, Page, async_playwright
from sqlalchemy import select

from app.models.runtime_v1 import BrowserSessionState
from app.services.web_tool_service import _resolve_public, _validate_url


class BrowserWorkerError(RuntimeError):
    def __init__(self, message: str, *, code: str = "browser_error", category: str = "provider"):
        self.code = code
        self.category = category
        super().__init__(message)


class BrowserOutcomeUnknown(BrowserWorkerError):
    def __init__(self, message: str):
        super().__init__(message, code="browser_outcome_unknown", category="outcome_unknown")


class PinnedEgressProxy:
    """Tiny loopback proxy that resolves policy-approved hosts exactly once.

    Chromium performs TLS to the original hostname through CONNECT, while the
    proxy opens the underlying socket to the already validated IP. This keeps
    normal certificate verification and closes the Browser DNS-rebinding gap.
    """

    def __init__(self) -> None:
        self.server = None
        self.port: int | None = None

    async def start(self) -> int:
        if self.server is None:
            self.server = await asyncio.start_server(
                self._handle, "127.0.0.1", 0, limit=65_536,
            )
            self.port = int(self.server.sockets[0].getsockname()[1])
        return self.port

    @staticmethod
    async def _reply(writer, status: str) -> None:
        writer.write(
            f"HTTP/1.1 {status}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n".encode()
        )
        await writer.drain()

    @staticmethod
    async def _relay(reader, writer, *, limit: int = 100 * 1024 * 1024) -> None:
        transferred = 0
        try:
            while chunk := await reader.read(65_536):
                transferred += len(chunk)
                if transferred > limit:
                    break
                writer.write(chunk)
                await writer.drain()
        finally:
            writer.close()

    async def _handle(self, reader, writer) -> None:
        upstream_writer = None
        try:
            raw_headers = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), timeout=10,
            )
            if len(raw_headers) > 65_536:
                raise ValueError("proxy headers too large")
            decoded = raw_headers.decode("iso-8859-1")
            lines = decoded.split("\r\n")
            method, target, version = lines[0].split(" ", 2)
            headers = []
            for line in lines[1:]:
                if not line:
                    continue
                key, separator, value = line.partition(":")
                if not separator:
                    raise ValueError("invalid proxy header")
                headers.append((key.strip(), value.strip()))
            if method.upper() == "CONNECT":
                parsed_target = urlsplit(f"//{target}")
                if not parsed_target.hostname:
                    raise ValueError("invalid CONNECT target")
                port = parsed_target.port or 443
                connect_host = (
                    f"[{parsed_target.hostname}]"
                    if ":" in parsed_target.hostname else parsed_target.hostname
                )
                _validate_url(f"https://{connect_host}:{port}/")
                addresses = await _resolve_public(parsed_target.hostname, port)
                upstream_reader, upstream_writer = await asyncio.wait_for(
                    asyncio.open_connection(addresses[0], port), timeout=15,
                )
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                await asyncio.gather(
                    self._relay(reader, upstream_writer),
                    self._relay(upstream_reader, writer),
                )
                return

            parsed_target = urlsplit(target)
            hostname, scheme, port = _validate_url(target)
            if scheme != "http":
                raise ValueError("non-CONNECT proxy request must use HTTP")
            addresses = await _resolve_public(hostname, port)
            content_length = 0
            forwarded = []
            for key, value in headers:
                lowered = key.casefold()
                if lowered == "content-length":
                    content_length = int(value)
                if lowered not in {
                    "host", "connection", "proxy-connection",
                    "proxy-authorization", "transfer-encoding",
                }:
                    forwarded.append((key, value))
            if content_length < 0 or content_length > 10 * 1024 * 1024:
                raise ValueError("proxy request body too large")
            if any(key.casefold() == "transfer-encoding" for key, _ in headers):
                raise ValueError("chunked proxy requests are unsupported")
            body = await reader.readexactly(content_length) if content_length else b""
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(addresses[0], port), timeout=15,
            )
            path = parsed_target.path or "/"
            if parsed_target.query:
                path += f"?{parsed_target.query}"
            authority = parsed_target.netloc
            request_lines = [
                f"{method} {path} {version}", f"Host: {authority}",
                *[f"{key}: {value}" for key, value in forwarded],
                "Connection: close", "", "",
            ]
            upstream_writer.write("\r\n".join(request_lines).encode("iso-8859-1") + body)
            await upstream_writer.drain()
            await self._relay(upstream_reader, writer)
        except Exception:
            if not writer.is_closing():
                try:
                    await self._reply(writer, "502 Bad Gateway")
                except Exception:
                    pass
        finally:
            if upstream_writer is not None and not upstream_writer.is_closing():
                upstream_writer.close()
            if not writer.is_closing():
                writer.close()


@dataclass
class _LiveSession:
    context: BrowserContext
    page: Page


_ELEMENT_SNAPSHOT_SCRIPT = """(limit) => {
  const selector = 'a,button,input,textarea,select,[role],[contenteditable="true"]';
  const nodes = Array.from(document.querySelectorAll(selector)).filter((el) => {
    const style = window.getComputedStyle(el); const rect = el.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
  }).slice(0, limit);
  return nodes.map((el, index) => {
    const ref = `e${index + 1}`; el.setAttribute('data-myclaw-ref', ref);
    const tag = el.tagName.toLowerCase(); const type = el.getAttribute('type') || '';
    const name = el.getAttribute('aria-label') || el.innerText || el.getAttribute('placeholder') || el.getAttribute('name') || '';
    const role = el.getAttribute('role') || ({a:'link',button:'button',input:'textbox',textarea:'textbox',select:'combobox'}[tag] || tag);
    const form = el.closest('form');
    return {ref, tag, role, type, name: String(name).trim().slice(0,240), href: el.href || null,
      form_method: form ? String(form.method || 'get').toUpperCase() : null,
      form_action: form ? form.action : null};
  });
}"""


async def _element_snapshot(page: Page, limit: int) -> tuple[list[dict], str]:
    elements = await page.evaluate(_ELEMENT_SNAPSHOT_SCRIPT, limit)
    fingerprint = hashlib.sha256(
        json.dumps(elements, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    return elements, fingerprint


class BrowserWorkerManager:
    def __init__(self) -> None:
        self._playwright = None
        self._browser: Browser | None = None
        self._egress_proxy = PinnedEgressProxy()
        self._sessions: dict[str, _LiveSession] = {}
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}"

    async def _launch(self) -> Browser:
        if self._browser is None:
            proxy_port = await self._egress_proxy.start()
            self._playwright = await async_playwright().start()
            configured_executable = os.getenv("MYCLAW_CHROMIUM_EXECUTABLE")
            executable = configured_executable or (
                "/usr/bin/chromium" if Path("/usr/bin/chromium").is_file() else None
            )
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                executable_path=executable,
                args=[
                    "--disable-dev-shm-usage", "--no-first-run",
                    f"--proxy-server=http://127.0.0.1:{proxy_port}",
                    "--proxy-bypass-list=<-loopback>",
                ],
            )
        return self._browser

    async def _route(self, route) -> None:
        url = route.request.url
        if url.startswith(("data:", "blob:", "about:")):
            await route.continue_()
            return
        try:
            hostname, _, port = _validate_url(url)
            await _resolve_public(hostname, port)
        except Exception:
            await route.abort("blockedbyclient")
            return
        await route.continue_()

    async def _new_live(self, session_id: str) -> _LiveSession:
        browser = await self._launch()
        context = await browser.new_context(
            accept_downloads=True,
            service_workers="block",
            java_script_enabled=True,
        )
        await context.route("**/*", self._route)
        # WebSockets bypass normal HTTP request routing. Keep them disabled so
        # page script cannot use them as an ungoverned egress channel.
        await context.route_web_socket("**/*", lambda route: route.close(
            code=1008, reason="MyClaw Browser network policy",
        ))
        live = _LiveSession(context=context, page=await context.new_page())
        self._sessions[session_id] = live
        return live

    async def ensure(self, db, *, session_id: str, workspace_id: str | None, allow_recover: bool) -> tuple[_LiveSession, BrowserSessionState]:
        state = await db.scalar(select(BrowserSessionState).where(
            BrowserSessionState.session_id == session_id,
        ).with_for_update())
        live = self._sessions.get(session_id)
        if state is None:
            state = BrowserSessionState(
                session_id=session_id, workspace_id=workspace_id,
                worker_instance_id=self.worker_id, status="new", page_revision=0,
                last_operation={},
            )
            db.add(state)
            await db.flush()
        if live is None:
            if state.status == "ready" and not allow_recover:
                state.status = "outcome_unknown"
                state.last_operation = {**dict(state.last_operation or {}), "reason": "browser_worker_restarted"}
                state.version += 1
                await db.flush()
                raise BrowserOutcomeUnknown("isolated Browser Worker restarted before the interaction could be verified")
            live = await self._new_live(session_id)
            state.worker_instance_id = self.worker_id
            state.status = "ready"
            state.version += 1
            if allow_recover and state.current_url:
                try:
                    await live.page.goto(state.current_url, wait_until="domcontentloaded", timeout=20_000)
                except Exception:
                    state.status = "failed"
                    await db.flush()
                    raise BrowserWorkerError("Browser page could not be recovered", code="browser_recovery_failed")
            await db.flush()
        return live, state

    async def navigate(self, db, *, session_id: str, workspace_id: str | None, url: str, wait_until: str) -> dict:
        hostname, _, port = _validate_url(url)
        await _resolve_public(hostname, port)
        live, state = await self.ensure(
            db, session_id=session_id, workspace_id=workspace_id, allow_recover=True,
        )
        try:
            response = await live.page.goto(url, wait_until=wait_until, timeout=30_000)
        except Exception as error:
            state.status = "failed"
            state.last_operation = {"action": "navigate", "url": url, "error": type(error).__name__}
            state.version += 1
            await db.flush()
            raise BrowserWorkerError(f"Browser navigation failed: {type(error).__name__}", code="browser_navigation_failed") from error
        state.page_revision += 1
        state.current_url = live.page.url
        parsed = urlsplit(live.page.url)
        state.current_origin = f"{parsed.scheme}://{parsed.netloc}"
        state.dom_fingerprint = None
        state.last_operation = {"action": "navigate", "requested_url": url, "status": response.status if response else None}
        state.status = "ready"
        state.version += 1
        await db.flush()
        return {
            "url": live.page.url, "title": await live.page.title(),
            "status_code": response.status if response else None,
            "page_revision": state.page_revision, "origin": state.current_origin,
        }

    async def observe(self, db, *, session_id: str, workspace_id: str | None, max_elements: int) -> dict:
        live, state = await self.ensure(
            db, session_id=session_id, workspace_id=workspace_id, allow_recover=True,
        )
        elements, fingerprint = await _element_snapshot(live.page, max_elements)
        aria = await live.page.locator("body").aria_snapshot(timeout=10_000)
        aria_truncated = len(aria) > 100_000
        if aria_truncated:
            aria = aria[:100_000]
        state.page_revision += 1
        state.current_url = live.page.url
        parsed = urlsplit(live.page.url)
        state.current_origin = f"{parsed.scheme}://{parsed.netloc}"
        state.dom_fingerprint = fingerprint
        state.last_operation = {
            "action": "observe", "elements": elements,
            "max_elements": max_elements,
        }
        state.status = "ready"
        state.version += 1
        await db.flush()
        return {
            "url": live.page.url, "title": await live.page.title(),
            "origin": state.current_origin, "page_revision": state.page_revision,
            "dom_fingerprint": fingerprint, "accessibility": aria,
            "accessibility_truncated": aria_truncated, "elements": elements,
        }

    async def interact(self, db, *, session_id: str, workspace_id: str | None, args: dict, upload_path: Path | None, download_path: Path | None) -> dict:
        live, state = await self.ensure(
            db, session_id=session_id, workspace_id=workspace_id, allow_recover=False,
        )
        if int(args["page_revision"]) != state.page_revision:
            raise BrowserWorkerError("Browser page revision is stale; observe again", code="browser_revision_conflict", category="conflict")
        if args.get("dom_fingerprint") != state.dom_fingerprint:
            raise BrowserWorkerError("Browser DOM fingerprint changed; observe again", code="browser_dom_conflict", category="conflict")
        parsed_live = urlsplit(live.page.url)
        live_origin = f"{parsed_live.scheme}://{parsed_live.netloc}"
        if live.page.url != state.current_url or live_origin != state.current_origin:
            raise BrowserWorkerError(
                "Browser page changed since the latest observation; observe again",
                code="browser_page_conflict", category="conflict",
            )
        observed = state.last_operation or {}
        current_elements, current_fingerprint = await _element_snapshot(
            live.page, int(observed.get("max_elements") or 200),
        )
        if current_fingerprint != state.dom_fingerprint:
            state.dom_fingerprint = None
            state.page_revision += 1
            state.last_operation = {
                "action": "dom_conflict", "elements": current_elements,
                "reason": "live_dom_changed_after_observe",
            }
            state.version += 1
            await db.flush()
            raise BrowserWorkerError(
                "Browser DOM changed after observation; observe again",
                code="browser_dom_conflict", category="conflict",
            )
        action = args["action"]
        ref = args.get("element_ref")
        locator = live.page.locator(f'[data-myclaw-ref="{ref}"]') if ref else None
        if locator is not None and await locator.count() != 1:
            raise BrowserWorkerError(
                "Browser element ref is no longer unique; observe again",
                code="browser_element_conflict", category="conflict",
            )
        state.last_operation = {"action": action, "arguments_hash": hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()}
        await db.flush()
        try:
            downloaded = None
            if action == "click":
                await locator.click(timeout=15_000)
            elif action == "input":
                await locator.fill(args.get("text", ""), timeout=15_000)
            elif action == "select":
                await locator.select_option(args.get("value"), timeout=15_000)
            elif action == "press":
                await (locator or live.page).press(args["key"], timeout=15_000)
            elif action == "scroll":
                await live.page.mouse.wheel(int(args.get("delta_x", 0)), int(args.get("delta_y", 600)))
            elif action == "upload":
                if upload_path is None:
                    raise BrowserWorkerError("upload file is unavailable", code="browser_upload_missing", category="validation")
                await locator.set_input_files(str(upload_path), timeout=15_000)
            elif action == "download":
                if download_path is None:
                    raise BrowserWorkerError("download target is unavailable", code="browser_download_target", category="validation")
                async with live.page.expect_download(timeout=30_000) as info:
                    await locator.click(timeout=15_000)
                download = await info.value
                await download.save_as(str(download_path))
                if download_path.stat().st_size > 50 * 1024 * 1024:
                    download_path.unlink(missing_ok=True)
                    raise BrowserOutcomeUnknown(
                        "Browser download exceeded the 50 MiB limit after the interaction completed"
                    )
                downloaded = {"path": str(download_path), "suggested_filename": download.suggested_filename}
            else:
                raise BrowserWorkerError("unsupported Browser action", code="browser_action_invalid", category="validation")
            await live.page.wait_for_timeout(250)
        except BrowserWorkerError:
            raise
        except Exception as error:
            # Interact may already have submitted a form or triggered another
            # external side effect. Never replay it automatically.
            state.status = "outcome_unknown"
            state.version += 1
            await db.flush()
            raise BrowserOutcomeUnknown(f"Browser interaction outcome is unknown: {type(error).__name__}") from error
        state.page_revision += 1
        state.current_url = live.page.url
        parsed = urlsplit(live.page.url)
        state.current_origin = f"{parsed.scheme}://{parsed.netloc}"
        state.dom_fingerprint = None
        state.status = "ready"
        state.last_operation = {**state.last_operation, "completed": True, "download": downloaded}
        state.version += 1
        await db.flush()
        return {
            "action": action, "url": live.page.url, "origin": state.current_origin,
            "page_revision": state.page_revision, "download": downloaded,
        }

    async def screenshot(self, db, *, session_id: str, workspace_id: str | None, path: Path, full_page: bool) -> dict:
        live, state = await self.ensure(
            db, session_id=session_id, workspace_id=workspace_id, allow_recover=True,
        )
        await live.page.screenshot(path=str(path), full_page=full_page)
        state.page_revision += 1
        state.last_operation = {"action": "screenshot", "path": path.name}
        state.version += 1
        await db.flush()
        return {"url": live.page.url, "page_revision": state.page_revision, "path": str(path)}


browser_manager = BrowserWorkerManager()
