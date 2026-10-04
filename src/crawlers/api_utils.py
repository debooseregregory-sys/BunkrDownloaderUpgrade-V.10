"""Utilities for resolving and signing downloadable media URLs from the Bunkr platform.

This module provides:
- Extraction of runtime variables from HTML/inline scripts
- Fallback resolution of direct download endpoints for non-landing assets
- Construction of CDN media paths
- Retrieval of signed URLs via Bunkr signing API
- Robust retry logic with exponential backoff for network resilience
"""

from __future__ import annotations

import asyncio
import random
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import aiohttp

from src.config import BUNKR_API, DOWNLOAD_API, JS_VARS_COMP
from src.diagnostics import log_http_503

if TYPE_CHECKING:
    from bs4 import BeautifulSoup


_DEFAULT_MAX_RETRIES = 5
_DEFAULT_BASE_DELAY = 2.0
_DEFAULT_TIMEOUT = 30
_FILE_ID_RE = re.compile(r"^\d+$")


class ResolveFailure(RuntimeError):
    """A structured, user-safe failure raised while resolving a download URL."""

    def __init__(self, code: str, phase: str, reason: str, *, status: int | None = None):
        self.code = code
        self.phase = phase
        self.reason = reason
        self.status = status
        status_text = f" HTTP {status}" if status is not None else ""
        super().__init__(f"{code} — {phase}: {reason}{status_text}")


def _failure_for_response(phase: str, status: int) -> ResolveFailure:
    if status == 429:
        code = "RATE_LIMITED"
    elif status in (401, 403):
        code = "AUTH_FAILED" if phase == "API" else "TOKEN_FAILED"
    elif status == 404:
        code = "RESOLVE_FAILED"
    elif status >= 500:
        code = "TEMPORARY_RESOLVE_ERROR"
    else:
        code = "RESOLVE_FAILED"
    return ResolveFailure(code, phase, "Bunkr rejected the request", status=status)


async def _retry_delay(
    response, attempt: int, *, route: str = "API", phase: str = "API",
    filename: str = "unknown",
) -> None:
    delay = None
    retry_after_value = response.headers.get("Retry-After") if response is not None else None
    if response is not None and response.status == 429:
        try:
            delay = float(retry_after_value or "")
        except (TypeError, ValueError):
            try:
                retry_at = parsedate_to_datetime(retry_after_value or "")
                delay = max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                delay = None
    if delay is None:
        delay = min(_DEFAULT_BASE_DELAY * (2 ** attempt), 30) + random.uniform(0.1, 0.5)
    delay = min(max(delay, 0), 120)
    if response is not None and response.status == 503:
        log_http_503(
            route=route,
            url=getattr(response, "url", None) or (DOWNLOAD_API if route == "API" else BUNKR_API),
            filename=filename,
            retry=attempt + 1,
            maximum=_DEFAULT_MAX_RETRIES,
            retry_after=retry_after_value,
            wait=delay,
            fresh_signed_url=False,
            phase=phase,
        )
    await asyncio.sleep(delay)


def _log_final_503(response, *, route: str, phase: str, filename: str) -> None:
    if response is None or getattr(response, "status", None) != 503:
        return
    log_http_503(
        route=route,
        url=getattr(response, "url", None) or (DOWNLOAD_API if route == "API" else BUNKR_API),
        filename=filename,
        retry=_DEFAULT_MAX_RETRIES,
        maximum=_DEFAULT_MAX_RETRIES,
        retry_after=response.headers.get("Retry-After"),
        wait=0,
        fresh_signed_url=False,
        final=True,
        phase=phase,
        reason="resolve retry budget exhausted",
    )


def unescape_js_path(value: str) -> str:
    """Normalize JavaScript-escaped URL fragments."""
    return value.replace(r"\/", "/").replace(r"\\", "\\")


def extract_page_vars(soup: BeautifulSoup) -> dict[str, str]:
    """Extract CDN/runtime variables from inline script tags."""
    for script in soup.find_all("script"):
        if script.string and "var jsCDN" in script.string:
            matches = JS_VARS_COMP.findall(script.string)
            return {
                key: unescape_js_path(value).strip("'\"")
                for key, value in matches
            }

    return {}


def extract_file_id(soup: BeautifulSoup) -> str | None:
    """Extract a numeric ID only from known Bunkr file-ID markup locations."""
    candidates: list[str] = []
    for node in soup.find_all(True):
        value = node.get("data-file-id")
        if value is not None:
            candidates.append(str(value).strip())

        # data-id is used on download controls on some file pages. Ignore unrelated
        # data-id attributes so an album/card identifier cannot be mistaken for a file.
        href = str(node.get("href", ""))
        marker = " ".join(str(node.get(key, "")) for key in ("id", "class", "aria-label")).lower()
        if node.get("data-id") is not None and (
            "download" in marker
            or (node.name == "a" and re.search(r"/(?:f|file)/", href, re.I))
        ):
            candidates.append(str(node.get("data-id")).strip())
        match = re.search(r"/(?:f|file)/(\d+)(?:[/?#]|$)", href, re.I)
        if match:
            candidates.append(match.group(1))

        if node.name == "script" and node.string:
            # Accept only explicitly named file-ID assignments, never arbitrary numbers.
            candidates.extend(re.findall(
                r"\b(?:fileId|file_id|fileID)\s*[:=]\s*['\"]?(\d+)['\"]?",
                node.string,
            ))

    return next((value for value in candidates if _FILE_ID_RE.fullmatch(value)), None)


async def get_download_response(
    session: aiohttp.ClientSession,
    file_id: str,
) -> str | None:
    """Fetch unsigned download URL for non-landing page assets.

    Used for file types that do not expose CDN variables (e.g. archives, videos).

    Retries with exponential backoff on network-related failures.
    Raises a typed resolve failure after bounded retries so callers can preserve
    the failing phase and status.
    """
    for attempt in range(1, _DEFAULT_MAX_RETRIES + 1):
        response = None
        try:
            async with session.post(
                DOWNLOAD_API,
                json={"id": file_id},
                timeout=aiohttp.ClientTimeout(total=_DEFAULT_TIMEOUT),
            ) as response:
                status = getattr(response, "status", 200)
                if status >= 400:
                    raise _failure_for_response("API", status)
                data = await response.json()

            # Guard against unexpected API response shapes so that a schema change
            # raises a warning rather than an unhandled KeyError.
            if not isinstance(data, dict):
                raise ResolveFailure("RESOLVE_FAILED", "API", "unexpected API response format")
            base_url = data.get("mediafiles")
            path = data.get("path")

            if not base_url or not path:
                raise ResolveFailure("RESOLVE_FAILED", "API", "mediafiles/path is missing")

            if str(path).startswith(("https://", "http://")):
                return str(path)
            parsed_url = urlparse(str(base_url))
            path_url = urlparse(str(path))
            api_path = path_url.path if path_url.path.startswith("/") else f"/{path_url.path}"
            combined_query = parse_qsl(parsed_url.query, keep_blank_values=True)
            combined_query.extend(parse_qsl(path_url.query, keep_blank_values=True))
            return urlunparse(parsed_url._replace(
                path=api_path,
                params=path_url.params,
                query=urlencode(combined_query),
                fragment=path_url.fragment,
            ))

        except ResolveFailure as exc:
            if exc.code in ("RATE_LIMITED", "TEMPORARY_RESOLVE_ERROR") and attempt < _DEFAULT_MAX_RETRIES:
                await _retry_delay(
                    response, attempt - 1, route="API", phase="API", filename=file_id,
                )
                continue
            _log_final_503(response, route="API", phase="API", filename=file_id)
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if attempt < _DEFAULT_MAX_RETRIES:
                await _retry_delay(None, attempt - 1)
                continue
            raise ResolveFailure("TEMPORARY_RESOLVE_ERROR", "API", str(exc)) from exc

    raise ResolveFailure("TEMPORARY_RESOLVE_ERROR", "API", "API attempts exhausted")


async def get_api_response(
    session: aiohttp.ClientSession,
    item_url: str,
    soup: BeautifulSoup | None = None,
) -> str | None:
    """Resolve and sign a Bunkr media URL using CDN or fallback pipeline.

    Resolution strategy:
        1. Extract CDN base URL from inline JavaScript (jsCDN)
        2. If missing, fallback to direct download endpoint
        3. Build media path from available source
        4. Request signed URL token from signing API

    Retries the signing API call with exponential backoff on network failures.
    Returns None if the media URL cannot be resolved or all signing attempts
    fail, allowing the caller to skip the file without crashing the session.
    """
    page_vars = extract_page_vars(soup) if soup else {}
    cdn_url = page_vars.get("jsCDN")

    # Use the ID/API fallback whenever the page does not provide a usable CDN URL.
    file_id = extract_file_id(soup) if soup and not cdn_url else None
    unsigned_url = await get_download_response(session, file_id) if file_id else None

    if not cdn_url and not unsigned_url:
        file_id = extract_file_id(soup) if soup else None
        reason = "file ID could not be determined" if not file_id else "API did not return a media URL"
        raise ResolveFailure("RESOLVE_FAILED", "RESOLVE", reason)

    base_url = cdn_url or unsigned_url
    parsed_media = urlparse(base_url)
    media_path = parsed_media.path + (f"?{parsed_media.query}" if parsed_media.query else "")
    if not media_path or media_path == "/":
        raise ResolveFailure("RESOLVE_FAILED", "API", "resolved media path is empty")

    for attempt in range(1, _DEFAULT_MAX_RETRIES + 1):
        response = None
        try:
            async with session.get(
                BUNKR_API,
                params={"path": media_path},
                timeout=aiohttp.ClientTimeout(total=_DEFAULT_TIMEOUT),
            ) as response:
                status = getattr(response, "status", 200)
                if status >= 400:
                    raise _failure_for_response("SIGN", status)
                data = await response.json()

        except ResolveFailure as exc:
            if exc.code in ("RATE_LIMITED", "TEMPORARY_RESOLVE_ERROR") and attempt < _DEFAULT_MAX_RETRIES:
                await _retry_delay(
                    response,
                    attempt - 1,
                    route="SIGNING",
                    phase="SIGNING",
                    filename=file_id or "resolved media",
                )
                continue
            _log_final_503(
                response, route="SIGNING", phase="SIGNING",
                filename=file_id or "resolved media",
            )
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if attempt < _DEFAULT_MAX_RETRIES:
                await _retry_delay(None, attempt - 1)
                continue
            raise ResolveFailure("TEMPORARY_RESOLVE_ERROR", "SIGN", str(exc)) from exc

        if not isinstance(data, dict):
            raise ResolveFailure("TOKEN_FAILED", "SIGN", "unexpected signing response format")

        token = data.get("token")
        expires_at = data.get("ex")
        if token and expires_at and base_url:
            parsed_base = urlparse(base_url)
            query = [
                (key, value)
                for key, value in parse_qsl(parsed_base.query, keep_blank_values=True)
                if key not in {"token", "ex"}
            ]
            query.extend((("token", str(token)), ("ex", str(expires_at))))
            return urlunparse(parsed_base._replace(query=urlencode(query)))

        raise ResolveFailure("TOKEN_FAILED", "SIGN", "signed token or expiry is missing")

    raise ResolveFailure("TEMPORARY_RESOLVE_ERROR", "SIGN", "signing attempts exhausted")
