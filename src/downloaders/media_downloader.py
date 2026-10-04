"""Module that provides tools to manage the downloading of individual files from Bunkr.

It supports retry mechanisms, progress tracking, and error handling for a robust
download experience.
"""

from __future__ import annotations

import random
import os
import threading
import time
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING
from collections.abc import Callable

import requests
from requests import RequestException

from src.diagnostics import (
    log_cdn_503_stage,
    log_download_start,
    log_http_503,
    track_download,
)
from src.config import (
    DOWNLOAD_HEADERS,
    CompletedReason,
    DownloadConfig,
    DownloadInfo,
    FailedReason,
    HTTPStatus,
    RetryConfig,
    SessionInfo,
    SkippedReason,
)
from src.file_utils import (
    matches_ignore_list,
    matches_include_list,
    truncate_filename,
    write_on_session_log,
)

from .download_utils import (
    detect_range_support,
    save_file_with_chunks,
    save_file_with_progress,
    should_use_parallel_download,
)

if TYPE_CHECKING:
    from src.managers.live_manager import LiveManager


CDN_503_BASE_DELAY = 15.0
CDN_503_MAX_DELAY = 180.0
CDN_503_JITTER = (2.0, 5.0)


_PATH_LOCKS: dict[str, tuple[threading.Lock, int]] = {}
_PATH_LOCKS_GUARD = threading.Lock()


@contextmanager
def _lock_download_path(path: Path):
    """Serialize writers for one destination path within this process."""
    key = os.path.normcase(os.path.abspath(str(path)))
    with _PATH_LOCKS_GUARD:
        lock, users = _PATH_LOCKS.get(key, (threading.Lock(), 0))
        _PATH_LOCKS[key] = (lock, users + 1)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
        with _PATH_LOCKS_GUARD:
            current_lock, users = _PATH_LOCKS[key]
            if users <= 1:
                del _PATH_LOCKS[key]
            else:
                _PATH_LOCKS[key] = (current_lock, users - 1)


class MediaDownloader:
    """Manage the downloading of individual files from Bunkr URLs."""

    def __init__(
        self,
        session_info: SessionInfo,
        download_info: DownloadInfo,
        live_manager: LiveManager,
        retry_config: RetryConfig,
        refresh_download_link: Callable[[], str] | None = None,
    ) -> None:
        """Initialize the MediaDownloader instance."""
        self.session_info = session_info
        self.download_info = download_info
        self.live_manager = live_manager
        self.retry_config = retry_config
        self.refresh_download_link = refresh_download_link
        self._suppress_external_retry = False

    def attempt_download(self, final_path: str) -> bool | None:
        """Track one active media transfer and emit a token-safe start record."""
        connections = getattr(self.session_info.args, "connections", 1)
        with track_download():
            log_download_start(
                self.download_info.filename,
                self.download_info.download_link,
                chunks=connections,
            )
            return self._attempt_download(final_path)

    def _attempt_download(self, final_path: str) -> bool | None:
        """Attempt to download the file, using parallel chunks when possible.

        If the server supports byte-range requests and the file is large enough, the
        download is split into num_connections parallel chunks (each saved as a .partN
        file to allow resuming). Falls back to the original single-connection stream
        when chunking is not applicable.

        The chunked path honors the same outer retry budget (--max-retries) as the
        single-connection fallback: a persistent failure (all internal per-chunk retries
        exhausted) triggers the same exponential back-off and re-attempt cycle via
        _retry_with_backoff, instead of giving up after a single call.

        Returns True if the download failed, False on success.
        """
        num_connections = getattr(self.session_info.args, "connections", 1)
        rate_limiter = self.session_info.rate_limiter

        for attempt in range(self.retry_config.retries):
            request_route = "MEDIA_GET"
            try:
                supports_range, content_length = detect_range_support(
                    self.download_info.download_link,
                    DOWNLOAD_HEADERS,
                    filename=self.download_info.filename,
                    retry=attempt + 1,
                    maximum=self.retry_config.retries,
                )

                if should_use_parallel_download(
                    content_length, num_connections, supports_range=supports_range,
                ):
                    request_route = "CHUNK_GET"
                    # .partN files are preserved on failure so a re-attempt
                    # (here or on a future run) resumes instead of restarting.
                    chunked_failed = save_file_with_chunks(
                        self.download_info.download_link,
                        final_path,
                        self.download_info.task,
                        self.live_manager,
                        DownloadConfig(
                            content_length=content_length,
                            num_connections=num_connections,
                            headers=DOWNLOAD_HEADERS,
                            rate_limiter=rate_limiter,
                        ),
                    )
                    if not chunked_failed:
                        return False

                    # Persistent failure after CHUNK_MAX_RETRIES internal
                    # attempts -- consume one outer retry slot, same as a
                    # request-level failure on the fallback path below.
                    if not self._retry_with_backoff(
                        attempt, event="Retrying chunked download",
                    ):
                        break

                    continue

                # Fallback: single-connection streaming download
                response = requests.get(
                    self.download_info.download_link,
                    stream=True,
                    headers=DOWNLOAD_HEADERS,
                    timeout=30,
                    allow_redirects=True,
                )
                response.raise_for_status()

            except RequestException as req_err:
                response = getattr(req_err, "response", None)
                if response is not None:
                    response.close()
                status = response.status_code if response is not None else None
                if status in (401, 403) and self._refresh_after_auth_failure(attempt):
                    continue
                if status == 503:
                    if not self._handle_request_exception(
                        req_err, attempt, route=request_route,
                    ):
                        self._suppress_external_retry = True
                        self.download_info.download_link = ""
                        break
                    if not self._refresh_after_cdn_503(attempt, request_route):
                        self._suppress_external_retry = True
                        break
                    continue
                if not self._handle_request_exception(req_err, attempt):
                    break

            else:
                outcome = save_file_with_progress(
                    response,
                    final_path,
                    self.download_info.task,
                    self.live_manager,
                    rate_limiter=rate_limiter,
                )
                if outcome is None:
                    return None
                if not outcome:
                    return False
                if not self._retry_with_backoff(attempt, event="Retrying incomplete download"):
                    break

        return True

    def _refresh_after_auth_failure(self, attempt: int) -> bool:
        """Resolve a new signed URL after a plausible token/auth rejection."""
        rejected_link = self.download_info.download_link
        # A rejected signature must never leak into the album-level retry pass.
        self.download_info.download_link = ""
        if self.refresh_download_link is None or (
            attempt >= self.retry_config.retries - 1
            and not self.retry_config.has_external_retry
        ):
            return False
        try:
            fresh_link = self.refresh_download_link()
        except Exception as exc:  # resolver failures are logged and the old link is discarded
            self.live_manager.update_log(
                event="Resolve failed after authorization error",
                details=f"{self.download_info.filename}: {exc}",
            )
            return False
        if not fresh_link or fresh_link == rejected_link:
            self.live_manager.update_log(
                event="Token refresh failed",
                details=f"No new signed URL was returned for {self.download_info.filename}.",
            )
            return False
        self.download_info.download_link = fresh_link
        return self._retry_with_backoff(attempt, event="Retrying after fresh resolve")

    def _refresh_after_cdn_503(self, attempt: int, route: str) -> bool:
        """Resolve the item page, API metadata and signature before retrying a 503."""
        rejected_link = self.download_info.download_link
        self.download_info.download_link = ""
        log_cdn_503_stage(
            "503 CDN -> fresh resolve",
            filename=self.download_info.filename,
            url=rejected_link,
            retry=attempt + 2,
            maximum=self.retry_config.retries,
            route=route,
        )

        def log_resolve_abandoned(reason: str) -> None:
            log_http_503(
                route=route,
                url=rejected_link,
                cdn_url=rejected_link,
                filename=self.download_info.filename,
                retry=attempt + 1,
                maximum=self.retry_config.retries,
                retry_after=None,
                wait=0,
                fresh_signed_url=False,
                final=True,
                reason=reason,
            )

        if self.refresh_download_link is None:
            self.live_manager.update_log(
                event="503 CDN resolve failed",
                details=f"No resolver is available for {self.download_info.filename}.",
            )
            log_resolve_abandoned("fresh resolver unavailable")
            return False
        try:
            fresh_link = self.refresh_download_link()
        except Exception as exc:
            self.live_manager.update_log(
                event="503 CDN resolve failed",
                details=f"Fresh resolve failed for {self.download_info.filename}: {exc}",
            )
            log_resolve_abandoned("fresh resolve failed")
            return False
        if not fresh_link or fresh_link == rejected_link:
            self.live_manager.update_log(
                event="503 CDN resolve failed",
                details=f"No different signed URL was returned for {self.download_info.filename}.",
            )
            log_resolve_abandoned("no different signed URL returned")
            return False

        self.download_info.download_link = fresh_link
        log_cdn_503_stage(
            "fresh signed URL obtained",
            filename=self.download_info.filename,
            url=fresh_link,
            retry=attempt + 2,
            maximum=self.retry_config.retries,
            route=route,
        )
        log_cdn_503_stage(
            f"retrying {route}",
            filename=self.download_info.filename,
            url=fresh_link,
            retry=attempt + 2,
            maximum=self.retry_config.retries,
            route=route,
        )
        return True

    def download(self) -> bool:
        """Handle the download process.

        Returns:
            True if the item ultimately failed (and no one else will retry it),
            False if it succeeded, was skipped, or will be retried later by an external
            caller (has_external_retry=True).

        """
        is_final_attempt = not self.retry_config.has_external_retry
        formatted_filename = truncate_filename(self.download_info.filename)
        final_path = Path(self.session_info.download_path) / formatted_filename

        # Skip download if the file exists or is blacklisted
        if self._skip_file_download(final_path):
            return False

        # Attempt to download the file with retries
        try:
            with _lock_download_path(final_path):
                # Another album item may have finished the same target while this
                # task was resolving its signed URL.
                if self._skip_file_download(final_path):
                    return False
                failed_download = self.attempt_download(final_path)

        except requests.exceptions.ConnectionError:
            self.live_manager.update_log(
                event="Connection error",
                details=f"Read timed out for {self.download_info.filename}",
            )
            failed_download = True

        if failed_download is None:
            # A concurrent process may have completed this target after our initial
            # check. Re-run the normal, user-visible skip accounting.
            self._skip_file_download(final_path)
            return False

        # Handle failed download after retries
        if failed_download:
            return self._handle_failed_download(
                is_final_attempt=is_final_attempt or self._suppress_external_retry,
            )

        self.live_manager.update_summary(CompletedReason.DOWNLOAD_SUCCESS)
        return False

    # Private methods
    def _skip_file_download(self, final_path: str) -> bool:
        """Determine whether a file should be skipped during download.

        This method checks the following conditions:
        - If the file already exists at the specified path.
        - If the file's name matches any pattern in the ignore list.
        - If the file's name does not match any pattern in the include list.

        If any of these conditions are met, the download is skipped, and appropriate
        logs are updated.
        """
        ignore_list = getattr(self.session_info.args, "ignore", [])
        include_list = getattr(self.session_info.args, "include", [])

        def log_and_skip_event(reason: str) -> bool:
            """Log the skip reason and updates the task before."""
            self.live_manager.update_log(event="Skipped download", details=reason)
            self.live_manager.update_task(
                self.download_info.task,
                completed=100,
                visible=False,
            )
            return True

        # Check if the file already exists
        existing_path = Path(final_path)
        if existing_path.exists() and existing_path.is_file():
            try:
                existing_size = existing_path.stat().st_size
            except OSError:
                existing_size = -1
            if existing_size > 0:
                self.live_manager.update_summary(SkippedReason.ALREADY_DOWNLOADED)
                return log_and_skip_event(
                    f"{self.download_info.filename} has already been downloaded."
                )
            self.live_manager.update_log(
                event="Incomplete existing file",
                details=(
                    f"{self.download_info.filename} is zero bytes and was preserved. "
                    "Remove it manually before retrying; existing paths are never overwritten."
                ),
            )
            self.live_manager.update_summary(SkippedReason.SERVICE_UNAVAILABLE)
            return log_and_skip_event(
                f"Preserved incomplete destination {existing_path}; it was not overwritten."
            )
        elif existing_path.exists():
            self.live_manager.update_log(
                event="Destination conflict",
                details=f"The target path is not a file: {existing_path}",
            )
            self.live_manager.update_summary(SkippedReason.SERVICE_UNAVAILABLE)
            return log_and_skip_event(f"Cannot download to non-file path {existing_path}.")

        # Check if the file is in the ignore list
        if matches_ignore_list(self.download_info.filename, ignore_list):
            self.live_manager.update_summary(SkippedReason.IGNORE_LIST)
            return log_and_skip_event(
                f"{self.download_info.filename} matches the ignore list.",
            )

        # Check if the file is not in the include list
        if matches_include_list(self.download_info.filename, include_list):
            self.live_manager.update_summary(SkippedReason.INCLUDE_LIST)
            return log_and_skip_event(
                f"No included words found for {self.download_info.filename}.",
            )

        # If none of the skip conditions are met, do not skip
        return False

    def _retry_with_backoff(
        self, attempt: int, *, event: str, retry_after: float | None = None,
        status_code: int | None = None, request_url: str | None = None,
        retry_after_value: str | None = None, route: str = "MEDIA_GET",
    ) -> bool:
        """Log error, apply backoff, and return True if should retry."""
        self.live_manager.update_log(
            event=event,
            details=f"{event} for {self.download_info.filename} "
            f"({attempt + 1}/{self.retry_config.retries})...",
        )

        if attempt < self.retry_config.retries - 1:
            if status_code == 503:
                delay = (
                    min(max(retry_after, CDN_503_BASE_DELAY), CDN_503_MAX_DELAY)
                    if retry_after is not None
                    else min(CDN_503_BASE_DELAY * (2 ** attempt), 150.0)
                    + random.uniform(*CDN_503_JITTER)  # noqa: S311
                )
                delay = min(delay, CDN_503_MAX_DELAY)
            else:
                delay = (
                    min(max(retry_after, 0.0), 120.0)
                    if retry_after is not None
                    else min(2 ** attempt, 30) + random.uniform(0.25, 1.0)  # noqa: S311
                )
            if status_code == 503:
                log_http_503(
                    route=route,
                    url=request_url or self.download_info.download_link,
                    cdn_url=self.download_info.download_link,
                    filename=self.download_info.filename,
                    retry=attempt + 1,
                    maximum=self.retry_config.retries,
                    retry_after=retry_after_value,
                    wait=delay,
                    fresh_signed_url=False,
                )
                log_cdn_503_stage(
                    "503 CDN -> waiting",
                    filename=self.download_info.filename,
                    url=self.download_info.download_link,
                    retry=attempt + 1,
                    maximum=self.retry_config.retries,
                    route=route,
                )
            time.sleep(delay)
            return True

        if status_code == 503:
            log_http_503(
                route=route,
                url=request_url or self.download_info.download_link,
                cdn_url=self.download_info.download_link,
                filename=self.download_info.filename,
                retry=self.retry_config.retries,
                maximum=self.retry_config.retries,
                retry_after=retry_after_value,
                wait=0,
                fresh_signed_url=False,
                final=True,
                reason="retry budget exhausted",
            )
        return False

    def _handle_request_exception(
        self, req_err: RequestException, attempt: int, *, route: str = "MEDIA_GET",
    ) -> bool:
        """Handle exceptions during the request and manages retries."""
        response = req_err.response
        status = response.status_code if response is not None else None
        if status == HTTPStatus.TOO_MANY_REQUESTS or (status is not None and 500 <= status <= 599) or response is None:
            retry_after = None
            retry_after_value = None
            if response is not None:
                retry_after_value = response.headers.get("Retry-After")
                try:
                    retry_after = float(retry_after_value or "")
                except (TypeError, ValueError):
                    try:
                        retry_at = parsedate_to_datetime(retry_after_value or "")
                        retry_after = max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
                    except (TypeError, ValueError, OverflowError):
                        retry_after = None
            self.live_manager.update_log(
                event="503 CDN -> waiting" if status == 503 else "Retrying temporary server/network error",
                details=f"HTTP {status or 'connection error'} for {self.download_info.filename}.",
            )
            return self._retry_with_backoff(
                attempt,
                event="Retrying download",
                retry_after=retry_after,
                status_code=status,
                request_url=getattr(response, "url", None) if response is not None else None,
                retry_after_value=retry_after_value,
                route=route,
            )

        if status == 404:
            self.live_manager.update_log(
                event="HTTP 404 - not found",
                details=f"The server reports that {self.download_info.filename} was not found.",
            )
            return False

        if status in (401, HTTPStatus.FORBIDDEN):
            self.live_manager.update_log(
                event="DOWNLOAD_FAILED — authorization",
                details=(
                    f"HTTP {status} after resolve for {self.download_info.filename}; "
                    "the fresh resolve was unavailable or exhausted."
                ),
            )
            return False

        # Do not retry, exit the loop
        self.live_manager.update_log(event="Request error", details=str(req_err))
        return False

    def _handle_failed_download(self, *, is_final_attempt: bool) -> bool:
        """Handle a failed download after all retry attempts.

        Always returns True (failed). When this is not the final attempt, only a log
        line is emitted -- the caller (AlbumDownloader) already has everything it needs
        to retry the item itself. The session log is only written on the final attempt,
        since that is the only point at which the outcome is permanent.
        """
        if not is_final_attempt:
            self.live_manager.update_log(
                event="Exceeded retry attempts",
                details=f"Max retries reached for {self.download_info.filename}. "
                "It will be retried one more time after all other tasks.",
            )
            # Hide stalled rows until retries to avoid cluttering the UI during large
            # albums. Rows reappear automatically when retries make progress.
            self.live_manager.update_task(
                self.download_info.task,
                completed=None,
                visible=False,
            )
            return True

        self.live_manager.update_log(
            event="Download failed",
            details=f"Failed to download {self.download_info.filename}. "
            "Check the log file.",
        )
        self._finalize_download(FailedReason.MAX_RETRIES_REACHED)
        return True

    def _finalize_download(
        self,
        reason: FailedReason | SkippedReason,
        *,
        completed: int | None = 100,
    ) -> None:
        outcome = reason.__class__.__name__.replace("Reason", "")

        write_on_session_log(
            self.download_info,
            reason=reason,
            outcome=outcome,
        )
        self.live_manager.update_task(
            self.download_info.task,
            completed=completed,
            visible=False,
        )
        self.live_manager.update_summary(reason)
