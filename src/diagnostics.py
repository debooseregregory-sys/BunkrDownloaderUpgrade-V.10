"""Safe, temporary diagnostics for HTTP failures during downloads."""

from __future__ import annotations

import logging
import threading
from contextvars import ContextVar
from contextlib import contextmanager
from contextlib import asynccontextmanager
from datetime import datetime
from urllib.parse import parse_qsl, urlparse

_lock = threading.Lock()
_active_downloads = 0
_active_chunk_requests = 0
_download_context = ContextVar("diagnostic_download_context", default=False)
_SIGNED_QUERY_KEYS = {"token", "sig", "signature", "ex", "expires", "auth"}


def safe_url_info(url: object) -> tuple[str, str, bool]:
    """Return hostname, path and signed-query presence without exposing query data."""
    try:
        parsed = urlparse(str(url))
        host = parsed.hostname or "unknown"
        path = parsed.path or "/"
        signed = any(key.lower() in _SIGNED_QUERY_KEYS
                     for key, _ in parse_qsl(parsed.query, keep_blank_values=True))
        return host, path, signed
    except (TypeError, ValueError):
        return "unknown", "/", False


def _safe_label(value: object, limit: int = 80) -> str:
    text = str(value or "unknown").replace("\r", " ").replace("\n", " ").replace("|", "/")
    return text[:limit]


def active_counts() -> tuple[int, int]:
    with _lock:
        return _active_downloads, _active_chunk_requests


@contextmanager
def track_download():
    global _active_downloads
    if _download_context.get():
        yield
        return
    context_token = _download_context.set(True)
    with _lock:
        _active_downloads += 1
    try:
        yield
    finally:
        with _lock:
            _active_downloads -= 1
        _download_context.reset(context_token)


@asynccontextmanager
async def track_download_async():
    with track_download():
        yield


@contextmanager
def track_chunk_request():
    global _active_chunk_requests
    with _lock:
        _active_chunk_requests += 1
    try:
        yield
    finally:
        with _lock:
            _active_chunk_requests -= 1


def log_download_start(filename: object, url: object, *, chunks: int) -> None:
    host, _, signed = safe_url_info(url)
    active, _ = active_counts()
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    logging.info(
        "%s DOWNLOAD START | file=%s | host=%s | signed=%s | workers=%s | chunks=%s",
        timestamp, _safe_label(filename), host, str(signed).lower(), active, chunks,
    )


def log_cdn_503_stage(
    stage: str, *, filename: object, url: object, retry: int, maximum: int,
    route: str = "MEDIA_GET",
) -> None:
    host, path, signed = safe_url_info(url)
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    logging.info(
        "%s %s | route=%s | host=%s | path=%s | file=%s | retry=%s/%s "
        "| signed=%s | fresh_signed_url=%s",
        timestamp,
        _safe_label(stage),
        _safe_label(route),
        _safe_label(host),
        _safe_label(path, 120),
        _safe_label(filename),
        retry,
        maximum,
        str(signed).lower(),
        str(stage == "fresh signed URL obtained" or stage.startswith("retrying ")).lower(),
    )


def log_http_503(
    *, route: str, url: object, filename: object, retry: int, maximum: int,
    retry_after: str | None, wait: float, fresh_signed_url: bool,
    cdn_url: object | None = None,
    final: bool = False, reason: str | None = None, active_chunks: int | None = None,
    phase: str | None = None,
) -> None:
    host, path, signed = safe_url_info(url)
    cdn_host, _, cdn_signed = safe_url_info(cdn_url if cdn_url is not None else url)
    active, current_chunks = active_counts()
    chunks = current_chunks if active_chunks is None else active_chunks
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    event = "503 FINAL" if final else "503"
    fields = [
        f"{timestamp} {event}", f"route={_safe_label(route)}", f"host={_safe_label(host)}",
        f"path={_safe_label(path, 120)}", "status=503",
        f"retry={retry}/{maximum}",
        f"retry_after={_safe_label(retry_after, 80) if retry_after is not None else 'absent'}",
        f"wait={wait:.2f}s", f"fresh_signed_url={str(fresh_signed_url).lower()}",
        f"signed={str(signed or cdn_signed).lower()}",
        f"cdn_host={cdn_host if cdn_signed else 'n/a'}",
        f"file={_safe_label(filename)}", f"active_downloads={active}",
        f"active_chunks={chunks}",
    ]
    if phase:
        fields.append(f"phase={_safe_label(phase)}")
    if reason:
        fields.append(f"reason={_safe_label(reason, 160)}")
    logging.warning(" | ".join(fields))
