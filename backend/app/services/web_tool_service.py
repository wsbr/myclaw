"""Real web Search/Fetch adapters with strict network safety controls."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import io
import json
import re
import socket
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader

from app.config import settings


BALANCED_SEARCH_RESULT_LIMIT = 8
BALANCED_FETCH_MAX_BYTES = 2 * 1024 * 1024
BALANCED_FETCH_MAX_TEXT_CHARS = 100_000
BALANCED_FETCH_TIMEOUT_SECONDS = 20

_FRESHNESS_ALIASES = {
    "day": "pd", "week": "pw", "month": "pm", "year": "py",
    "pd": "pd", "pw": "pw", "pm": "pm", "py": "py",
}
_TRACKING_QUERY_KEYS = {
    "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src", "spm",
}
_RELATIVE_TIME_PATTERN = re.compile(
    r"(?:今天|今日|昨天|昨日|本周|这周|最近|近期|当前|最新|今年|"
    r"\btoday\b|\byesterday\b|\bthis\s+week\b|\blast\s+week\b|"
    r"\brecent(?:ly)?\b|\bcurrent(?:ly)?\b|\blatest\b|\bthis\s+year\b)",
    re.IGNORECASE,
)
_SOFT_404_PATTERN = re.compile(
    r"(?:\b404\b|page\s+not\s+found|not\s+found|页面不存在|找不到页面|内容不存在)",
    re.IGNORECASE,
)
_COMMON_MULTIPART_SUFFIXES = {
    "co.uk", "com.au", "com.br", "com.cn", "com.hk", "com.sg", "co.jp",
    "co.kr", "co.nz", "org.uk", "net.cn", "org.cn",
}


class WebToolError(RuntimeError):
    def __init__(self, message: str, *, code: str, category: str = "provider", retryable: bool = False):
        self.code = code
        self.category = category
        self.retryable = retryable
        super().__init__(message)


def normalize_freshness(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = _FRESHNESS_ALIASES.get(str(value).casefold())
    if normalized is None:
        raise WebToolError(
            "freshness must be day, week, month, or year",
            code="invalid_freshness", category="validation",
        )
    return normalized


def _normalized_domain(value: str) -> str:
    candidate = value.strip().casefold().rstrip(".")
    if not candidate or len(candidate) > 253 or "/" in candidate or ":" in candidate:
        raise WebToolError(
            "domains must contain DNS hostnames only",
            code="invalid_search_domain", category="validation",
        )
    if any(
        not label or len(label) > 63 or not re.fullmatch(r"[a-z0-9-]+", label)
        or label.startswith("-") or label.endswith("-")
        for label in candidate.split(".")
    ):
        raise WebToolError(
            "domains must contain valid DNS hostnames",
            code="invalid_search_domain", category="validation",
        )
    return candidate


def effective_search_query(
    query: str, *, freshness: str | None = None,
    domains: list[str] | None = None, runtime_clock: dict | None = None,
) -> tuple[str, list[str]]:
    """Apply platform-owned date and domain constraints deterministically."""

    normalized_freshness = normalize_freshness(freshness)
    normalized_domains = list(dict.fromkeys(
        _normalized_domain(item) for item in list(domains or [])
    ))
    if len(normalized_domains) > 5:
        raise WebToolError(
            "at most five search domains are allowed",
            code="search_domain_limit_exceeded", category="validation",
        )
    parts = [query.strip()]
    clock = dict(runtime_clock or {})
    if (
        (normalized_freshness or _RELATIVE_TIME_PATTERN.search(query))
        and clock.get("local_date") and clock.get("timezone")
    ):
        local_date = date.fromisoformat(str(clock["local_date"]))
        lowered = query.casefold()
        lookback = {
            "pd": 0, "pw": 6, "pm": 30, "py": 365,
        }.get(normalized_freshness)
        if lookback is None:
            if re.search(r"昨天|昨日|\byesterday\b", lowered):
                local_date -= timedelta(days=1)
                lookback = 0
            elif re.search(r"最近一周|过去一周|本周|这周|\b(?:last|this)\s+week\b", lowered):
                lookback = 6
            elif re.search(r"最近一月|过去一月|最近一个月|\b(?:last|this)\s+month\b", lowered):
                lookback = 30
            elif re.search(r"今年|\bthis\s+year\b", lowered):
                lookback = (local_date - date(local_date.year, 1, 1)).days
            elif re.search(r"今天|今日|\btoday\b", lowered):
                lookback = 0
            else:
                lookback = 30
        start_date = local_date - timedelta(days=lookback)
        parts.append(
            f"Explicit date range ({clock['timezone']}): "
            f"{start_date.isoformat()} through {local_date.isoformat()}."
        )
    if normalized_domains:
        parts.append(
            "Source domain constraint: "
            + " OR ".join(f"site:{domain}" for domain in normalized_domains)
        )
    return "\n".join(parts), normalized_domains


def canonicalize_url(value: str) -> str:
    parsed = urlsplit(value)
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    port = parsed.port
    netloc = f"[{hostname}]" if ":" in hostname else hostname
    if port and port != (443 if parsed.scheme.casefold() == "https" else 80):
        netloc = f"[{hostname}]:{port}" if ":" in hostname else f"{hostname}:{port}"
    query = urlencode([
        (key, item) for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_")
        and key.casefold() not in _TRACKING_QUERY_KEYS
    ], doseq=True)
    return urlunsplit((
        parsed.scheme.casefold(), netloc, parsed.path or "/", query, "",
    ))


def _registrable_domain(url: str) -> str:
    host = (urlsplit(url).hostname or "").casefold().rstrip(".")
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    suffix = ".".join(labels[-2:])
    return ".".join(labels[-3:]) if suffix in _COMMON_MULTIPART_SUFFIXES else suffix


def _source_id(url: str) -> str:
    return "web-" + hashlib.sha256(url.encode()).hexdigest()[:20]


def _normalize_search_items(
    items: list[dict], *, domains: list[str] | None = None, limit: int = 8,
) -> list[dict]:
    allowed = set(domains or [])
    unique: list[dict] = []
    seen: set[str] = set()
    for item in items:
        try:
            original = str(item.get("url") or "")
            _validate_url(original)
            canonical = canonicalize_url(original)
            _validate_url(canonical)
        except (ValueError, WebToolError):
            continue
        host = (urlsplit(canonical).hostname or "").casefold()
        if allowed and not any(host == domain or host.endswith("." + domain) for domain in allowed):
            continue
        if canonical in seen:
            continue
        seen.add(canonical)
        unique.append({
            **item,
            "url": canonical,
            "source_id": _source_id(canonical),
            "site_name": host,
        })
    selected: list[dict] = []
    deferred: list[dict] = []
    domain_counts: dict[str, int] = {}
    for item in unique:
        domain = _registrable_domain(item["url"])
        if domain_counts.get(domain, 0) >= 2:
            deferred.append(item)
            continue
        selected.append(item)
        domain_counts[domain] = domain_counts.get(domain, 0) + 1
        if len(selected) >= limit:
            return selected
    selected.extend(deferred[:max(0, limit - len(selected))])
    return selected[:limit]


async def _bounded_json_response(client, method: str, url: str, **kwargs):
    max_bytes = 5 * 1024 * 1024
    async with client.stream(method, url, **kwargs) as response:
        response.raise_for_status()
        advertised = response.headers.get("content-length")
        if advertised:
            try:
                advertised_size = int(advertised)
                if advertised_size < 0:
                    raise ValueError("negative Content-Length")
                if advertised_size > max_bytes:
                    raise WebToolError(
                        "Search Provider response exceeds the size limit",
                        code="search_response_too_large", category="provider",
                    )
            except ValueError as error:
                raise WebToolError(
                    "Search Provider returned an invalid Content-Length",
                    code="search_content_length_invalid", category="provider",
                ) from error
        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > max_bytes:
                raise WebToolError(
                    "Search Provider response exceeds the size limit",
                    code="search_response_too_large", category="provider",
                )
            body.extend(chunk)
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise WebToolError(
                "Search Provider returned invalid JSON",
                code="search_response_invalid_json", category="provider",
            ) from error
        if not isinstance(payload, dict):
            raise WebToolError(
                "Search Provider returned a non-object response",
                code="search_response_invalid_shape", category="provider",
            )
        return payload, response


def _public_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    return not any((
        address.is_private, address.is_loopback, address.is_link_local,
        address.is_multicast, address.is_reserved, address.is_unspecified,
    ))


def _trusted_synthetic_address(value: str, hostname: str) -> bool:
    """Allow an explicitly configured egress-gateway synthetic IP for a DNS name.

    This never applies to literal-IP URLs. The trusted gateway is responsible
    for mapping the synthetic address to the already validated hostname.
    """

    try:
        ipaddress.ip_address(hostname)
        return False
    except ValueError:
        pass
    address = ipaddress.ip_address(value)
    return any(
        address in ipaddress.ip_network(cidr, strict=True)
        for cidr in settings.trusted_synthetic_dns_cidrs
    )


async def _resolve_public(hostname: str, port: int) -> list[str]:
    try:
        records = await asyncio.get_running_loop().getaddrinfo(
            hostname, port, type=socket.SOCK_STREAM,
        )
    except socket.gaierror as error:
        raise WebToolError(
            "URL hostname could not be resolved",
            code="dns_resolution_failed", category="provider", retryable=False,
        ) from error
    addresses = sorted({record[4][0] for record in records})
    if not addresses or any(
        not (_public_address(item) or _trusted_synthetic_address(item, hostname))
        for item in addresses
    ):
        raise WebToolError("URL resolves to a prohibited network", code="ssrf_prohibited_address", category="authorization")
    return addresses


def _validate_url(value: str) -> tuple[str, str, int]:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise WebToolError("URL must be absolute HTTP(S)", code="invalid_url", category="validation")
    if parsed.username or parsed.password:
        raise WebToolError("URL must not contain credentials", code="url_credentials_prohibited", category="authorization")
    lowered_host = parsed.hostname.rstrip(".").lower()
    if (
        lowered_host in {
            "localhost", "localhost.localdomain", "metadata.google.internal",
            "metadata.aws.internal", "instance-data.ec2.internal",
        }
        or lowered_host.endswith((".localhost", ".local", ".internal"))
    ):
        raise WebToolError("local hostnames are prohibited", code="ssrf_prohibited_host", category="authorization")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as error:
        raise WebToolError(
            "URL contains an invalid port", code="invalid_url", category="validation",
        ) from error
    return parsed.hostname, parsed.scheme, port


def _normalized_lines(value: str) -> list[str]:
    return [
        re.sub(r"\s+", " ", line).strip()
        for line in value.splitlines()
        if re.sub(r"\s+", " ", line).strip()
    ]


def _objective_terms(value: str) -> set[str]:
    lowered = value.casefold()
    latin = set(re.findall(r"[a-z0-9][a-z0-9_.-]{1,}", lowered))
    cjk_groups = re.findall(r"[\u3400-\u9fff]+", lowered)
    cjk = {
        group[index:index + 2]
        for group in cjk_groups
        for index in range(max(1, len(group) - 1))
        if group[index:index + 2]
    }
    return latin | cjk


def select_relevant_excerpts(
    text: str, objective: str, *, max_chars: int = 16_000,
) -> list[str]:
    """Select bounded source passages without an untracked model call."""

    lines = _normalized_lines(text)
    if not lines:
        return []
    chunks: list[str] = []
    current: list[str] = []
    current_length = 0
    for line in lines:
        if current and current_length + len(line) + 1 > 1_200:
            chunks.append("\n".join(current))
            current, current_length = [], 0
        current.append(line)
        current_length += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    terms = _objective_terms(objective)
    ranked = []
    for index, chunk in enumerate(chunks):
        chunk_terms = _objective_terms(chunk)
        overlap = len(terms & chunk_terms)
        density = overlap / max(1, len(terms))
        ranked.append((overlap > 0, density, overlap, -index, index, chunk))
    ranked.sort(reverse=True)
    selected = []
    used = 0
    for has_match, _density, _overlap, _position, _index, chunk in ranked:
        if terms and not has_match and selected:
            continue
        remaining = max_chars - used
        if remaining <= 0:
            break
        selected.append(chunk[:remaining])
        used += min(len(chunk), remaining)
        if used >= max_chars:
            break
    if not selected:
        selected = ["\n".join(lines)[:max_chars]]
    return selected


def _looks_like_soft_404(*, final_url: str, title: str | None, text: str) -> bool:
    path = urlsplit(final_url).path.casefold()
    path_signal = bool(re.search(r"(?:^|[/_.-])404(?:[/_.-]|$)|not[-_]?found", path))
    title_signal = bool(_SOFT_404_PATTERN.search(title or ""))
    body_signal = bool(_SOFT_404_PATTERN.search(text[:2_000]))
    return sum((path_signal, title_signal, body_signal)) >= 2


async def safe_fetch(
    url: str, *, timeout_seconds: int = BALANCED_FETCH_TIMEOUT_SECONDS,
    max_bytes: int = BALANCED_FETCH_MAX_BYTES,
    max_redirects: int = 5,
    max_text_chars: int = BALANCED_FETCH_MAX_TEXT_CHARS,
    objective: str = "",
) -> dict:
    current = url
    redirects: list[str] = []
    started = time.perf_counter()
    async with httpx.AsyncClient(
        follow_redirects=False, timeout=httpx.Timeout(timeout_seconds),
        trust_env=False, headers={"User-Agent": "MyClaw-WebFetch/1.0"},
    ) as client:
        for redirect_no in range(max_redirects + 1):
            hostname, scheme, port = _validate_url(current)
            addresses = await _resolve_public(hostname, port)
            # Connect to the validated address and retain hostname for HTTP Host
            # and TLS SNI. This closes the DNS-rebinding window between policy
            # evaluation and socket establishment.
            selected = addresses[0]
            parsed = urlsplit(current)
            ip_host = f"[{selected}]" if ":" in selected else selected
            netloc = f"{ip_host}:{port}"
            pinned = urlunsplit((scheme, netloc, parsed.path or "/", parsed.query, ""))
            try:
                async with client.stream(
                    "GET", pinned,
                    headers={"Host": parsed.netloc},
                    extensions={"sni_hostname": hostname},
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise WebToolError("redirect omitted Location", code="invalid_redirect")
                        if redirect_no >= max_redirects:
                            raise WebToolError("redirect limit exceeded", code="redirect_limit", category="validation")
                        current = urljoin(current, location)
                        redirects.append(current)
                        continue
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                    if content_type not in {
                        "text/html", "text/plain", "application/pdf",
                        "application/xhtml+xml", "application/json",
                    }:
                        raise WebToolError("response MIME type is not permitted", code="mime_not_allowed", category="validation")
                    advertised = response.headers.get("content-length")
                    advertised_truncated = False
                    if advertised:
                        try:
                            advertised_size = int(advertised)
                        except ValueError as error:
                            raise WebToolError(
                                "origin returned an invalid Content-Length",
                                code="invalid_content_length", category="provider",
                            ) from error
                        if advertised_size < 0:
                            raise WebToolError(
                                "origin returned an invalid Content-Length",
                                code="invalid_content_length", category="provider",
                            )
                        advertised_truncated = advertised_size > max_bytes
                    body = bytearray()
                    truncated = advertised_truncated
                    async for chunk in response.aiter_bytes():
                        remaining = max_bytes - len(body)
                        if remaining <= 0:
                            truncated = True
                            break
                        body.extend(chunk[:remaining])
                        if len(chunk) > remaining:
                            truncated = True
                            break
                    final_url = current
                    headers = dict(response.headers)
                    status_code = response.status_code
                    break
            except httpx.TimeoutException as error:
                raise WebToolError("web fetch timed out", code="fetch_timeout", category="timeout", retryable=True) from error
            except httpx.HTTPStatusError as error:
                category = "rate_limit" if error.response.status_code == 429 else "provider"
                raise WebToolError(
                    f"origin returned HTTP {error.response.status_code}",
                    code=f"http_{error.response.status_code}", category=category,
                    retryable=error.response.status_code in {408, 429} or error.response.status_code >= 500,
                ) from error
        else:
            raise WebToolError("redirect limit exceeded", code="redirect_limit", category="validation")
    raw = bytes(body)
    if content_type == "application/pdf":
        try:
            reader = PdfReader(io.BytesIO(raw))
            if len(reader.pages) > 500:
                raise WebToolError(
                    "PDF page count exceeds extraction limit",
                    code="pdf_page_limit", category="validation",
                )
            text = "\n\n".join(page.extract_text() or "" for page in reader.pages)
        except WebToolError:
            raise
        except Exception as error:
            raise WebToolError("PDF extraction failed", code="pdf_parse_failed") from error
        title = None
    elif content_type in {"text/html", "application/xhtml+xml"}:
        soup = BeautifulSoup(raw, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else None
        for element in soup([
            "script", "style", "noscript", "template", "svg", "nav", "header",
            "footer", "aside", "form",
        ]):
            element.decompose()
        main = soup.find("article") or soup.find("main") or soup.body or soup
        text = "\n".join(_normalized_lines(main.get_text("\n")))
    else:
        title = None
        text = raw.decode("utf-8", errors="replace")
    text_length = len(text)
    text_truncated = text_length > max_text_chars
    if text_truncated:
        text = text[:max_text_chars]
    if _looks_like_soft_404(final_url=final_url, title=title, text=text):
        raise WebToolError(
            "origin returned a soft 404 page",
            code="soft_404", category="validation", retryable=False,
        )
    normalized_url = canonicalize_url(final_url)
    return {
        "url": url, "final_url": normalized_url, "title": title,
        "status_code": status_code, "mime_type": content_type,
        "text": text, "checksum": hashlib.sha256(raw).hexdigest(),
        "normalized_text_checksum": hashlib.sha256(text.encode()).hexdigest(),
        "byte_size": len(raw), "text_length": text_length,
        "truncated": truncated or text_truncated,
        "raw_truncated": truncated, "text_truncated": text_truncated,
        "redirects": redirects, "elapsed_ms": round((time.perf_counter() - started) * 1000),
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provider_request_id": (
            headers.get("x-request-id") or headers.get("x-amzn-requestid")
            or headers.get("cf-ray")
        ),
        "source_id": _source_id(normalized_url),
        "objective": objective,
        "excerpts": select_relevant_excerpts(text, objective),
        "untrusted_external_content": True,
    }


async def web_search(
    *, provider: str, endpoint: str, api_key: str, query: str,
    count: int = BALANCED_SEARCH_RESULT_LIMIT,
    freshness: str | None = None, country: str | None = None,
    domains: list[str] | None = None,
    config: dict | None = None,
) -> tuple[list[dict], dict]:
    started = time.perf_counter()
    freshness = normalize_freshness(freshness)
    count = min(BALANCED_SEARCH_RESULT_LIMIT, max(1, int(count)))
    provider_result_count = min(20, count * 2)
    normalized_domains = list(dict.fromkeys(
        _normalized_domain(item) for item in list(domains or [])
    ))
    if len(normalized_domains) > 5:
        raise WebToolError(
            "at most five search domains are allowed",
            code="search_domain_limit_exceeded", category="validation",
        )
    headers = {"Accept": "application/json"}
    provider_config = dict(config or {})
    try:
        hostname, scheme, port = _validate_url(endpoint)
        addresses = await _resolve_public(hostname, port)
        selected = addresses[0]
        parsed_endpoint = urlsplit(endpoint)
        ip_host = f"[{selected}]" if ":" in selected else selected
        pinned_endpoint = urlunsplit((
            scheme, f"{ip_host}:{port}", parsed_endpoint.path or "/",
            parsed_endpoint.query, "",
        ))
        headers["Host"] = parsed_endpoint.netloc
        tls_extensions = {"sni_hostname": hostname}
        timeout_seconds = 60 if provider == "bailian" else 20
        async with httpx.AsyncClient(timeout=timeout_seconds, trust_env=False) as client:
            if provider == "brave":
                headers["X-Subscription-Token"] = api_key
                params = {"q": query, "count": provider_result_count}
                if freshness:
                    params["freshness"] = freshness
                if country:
                    params["country"] = country
                payload, response = await _bounded_json_response(
                    client, "GET", pinned_endpoint, params=params,
                    headers=headers, extensions=tls_extensions,
                )
                web_payload = payload.get("web") or {}
                if not isinstance(web_payload, dict):
                    raise WebToolError(
                        "Brave returned an invalid result shape",
                        code="search_response_invalid_shape", category="provider",
                    )
                source = web_payload.get("results") or []
                if not isinstance(source, list):
                    raise WebToolError(
                        "Brave returned an invalid result shape",
                        code="search_response_invalid_shape", category="provider",
                    )
                items = [{
                    "title": str(item.get("title") or "")[:500],
                    "url": str(item.get("url") or "")[:2000],
                    "snippet": str(item.get("description") or "")[:4000],
                    "published_at": str(item.get("age") or "")[:200] or None,
                } for item in source[:provider_result_count] if isinstance(item, dict)]
            elif provider == "bocha":
                headers["Authorization"] = f"Bearer {api_key}"
                request = {"query": query, "count": provider_result_count, "summary": True}
                if freshness:
                    # The platform contract uses the provider-neutral Brave-style
                    # age buckets. Bocha's public API uses named ranges instead.
                    request["freshness"] = {
                        "pd": "oneDay", "pw": "oneWeek",
                        "pm": "oneMonth", "py": "oneYear",
                    }[freshness]
                else:
                    request["freshness"] = "noLimit"
                if country:
                    raise WebToolError(
                        "Bocha does not support country filtering",
                        code="provider_parameter_unsupported", category="validation",
                    )
                payload, response = await _bounded_json_response(
                    client, "POST", pinned_endpoint, json=request,
                    headers=headers, extensions=tls_extensions,
                )
                business_code = payload.get("code")
                if business_code not in {None, 0, 200, "0", "200"}:
                    raise WebToolError(
                        "Bocha returned a Provider business error",
                        code=f"provider_business_{str(business_code)[:32]}",
                        category="provider", retryable=False,
                    )
                response_data = payload.get("data") or payload
                if not isinstance(response_data, dict):
                    raise WebToolError(
                        "Bocha returned an invalid result shape",
                        code="search_response_invalid_shape", category="provider",
                    )
                web_pages = response_data.get("webPages") or {}
                if not isinstance(web_pages, dict):
                    raise WebToolError(
                        "Bocha returned an invalid result shape",
                        code="search_response_invalid_shape", category="provider",
                    )
                source = web_pages.get("value") or []
                if not isinstance(source, list):
                    raise WebToolError(
                        "Bocha returned an invalid result shape",
                        code="search_response_invalid_shape", category="provider",
                    )
                items = [{
                    "title": str(item.get("name") or "")[:500],
                    "url": str(item.get("url") or "")[:2000],
                    "snippet": str(item.get("summary") or item.get("snippet") or "")[:4000],
                    "published_at": str(item.get("datePublished") or "")[:200] or None,
                } for item in source[:provider_result_count] if isinstance(item, dict)]
            elif provider == "bailian":
                if country:
                    raise WebToolError(
                        "Bailian does not support country filtering",
                        code="provider_parameter_unsupported", category="validation",
                    )
                headers["Authorization"] = f"Bearer {api_key}"
                search_options = {
                    "forced_search": True,
                    "enable_source": True,
                    "search_strategy": provider_config.get("search_strategy", "turbo"),
                }
                bailian_query = query
                if freshness == "pd":
                    # DashScope turbo documents only 7/30/180/365 as numeric
                    # freshness values. Preserve the platform's 24-hour
                    # semantics without sending the invalid value 1.
                    bailian_query = (
                        "仅检索过去24小时内发布的来源（严格时效约束）。\n" + query
                    )
                elif freshness:
                    search_options["freshness"] = {
                        "pw": 7, "pm": 30, "py": 365,
                    }[freshness]
                request = {
                    "model": provider_config.get("model", "qwen-plus"),
                    "input": {"messages": [{"role": "user", "content": bailian_query}]},
                    "parameters": {
                        "enable_search": True,
                        "search_options": search_options,
                        # The generated answer is intentionally discarded; cap
                        # it tightly while preserving structured search_info.
                        "max_tokens": 64,
                        "temperature": 0,
                        "result_format": "message",
                    },
                }
                payload, response = await _bounded_json_response(
                    client, "POST", pinned_endpoint, json=request,
                    headers=headers, extensions=tls_extensions,
                )
                business_code = payload.get("code")
                if business_code not in {None, 0, 200, "0", "200"}:
                    raise WebToolError(
                        "Bailian returned a Provider business error",
                        code=f"provider_business_{str(business_code)[:32]}",
                        category="provider", retryable=False,
                    )
                output = payload.get("output")
                if not isinstance(output, dict):
                    raise WebToolError(
                        "Bailian returned an invalid result shape",
                        code="search_response_invalid_shape", category="provider",
                    )
                search_info = output.get("search_info")
                if not isinstance(search_info, dict):
                    # A successful model response without search_info can be a
                    # fluent hallucination. Never publish it as Search evidence.
                    raise WebToolError(
                        "Bailian did not execute the forced web search",
                        code="provider_search_not_executed", category="provider",
                    )
                source = search_info.get("search_results")
                if not isinstance(source, list):
                    raise WebToolError(
                        "Bailian returned an invalid search result shape",
                        code="search_response_invalid_shape", category="provider",
                    )
                items = [{
                    "title": str(item.get("title") or "")[:500],
                    "url": str(item.get("url") or "")[:2000],
                    "snippet": str(
                        item.get("snippet") or item.get("content")
                        or item.get("summary") or ""
                    )[:4000],
                    "published_at": str(
                        item.get("publish_time") or item.get("published_at")
                        or item.get("date") or ""
                    )[:200] or None,
                } for item in source[:provider_result_count] if isinstance(item, dict)]
            else:
                raise WebToolError(
                    "unsupported Search Provider", code="search_provider_unsupported",
                    category="validation",
                )
    except httpx.TimeoutException as error:
        raise WebToolError("Search Provider timed out", code="search_timeout", category="timeout", retryable=True) from error
    except httpx.HTTPStatusError as error:
        status = error.response.status_code
        raise WebToolError(
            f"Search Provider returned HTTP {status}", code=f"provider_http_{status}",
            category="rate_limit" if status == 429 else "provider",
            retryable=status in {408, 429} or status >= 500,
        ) from error
    items = _normalize_search_items(
        items, domains=normalized_domains, limit=count,
    )
    usage_payload = payload.get("usage") if provider == "bailian" else None
    safe_usage = {
        key: value for key, value in (
            usage_payload.items() if isinstance(usage_payload, dict) else []
        )
        if key in {"input_tokens", "output_tokens", "total_tokens"}
        and isinstance(value, int) and value >= 0
    }
    return items, {
        "name": provider, "request_id": response.headers.get("x-request-id"),
        "latency_ms": round((time.perf_counter() - started) * 1000),
        **({
            "request_id": payload.get("request_id") or response.headers.get("x-request-id"),
            "model": provider_config.get("model", "qwen-plus"),
            "search_strategy": provider_config.get("search_strategy", "turbo"),
            "usage": safe_usage,
        } if provider == "bailian" else {}),
    }
