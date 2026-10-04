"""Utilities for fetching pages, managing directories, and clearing the terminal.

It includes functions to handle common tasks such as sending HTTP requests, parsing
HTML, creating download directories, and clearing the terminal, making it reusable
across projects.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import random
import shutil
import sys
from http.client import RemoteDisconnected
from pathlib import Path
from typing import TYPE_CHECKING

import requests
from bs4 import BeautifulSoup
from requests import Response

from .config import (
    DOWNLOAD_HEADERS,
    FETCH_ERROR_MESSAGES,
    GB,
    MIN_DISK_SPACE_GB,
    HTTPStatus,
)
from .diagnostics import log_http_503
from .url_utils import replace_domain_with_fallback

if TYPE_CHECKING:
    from src.managers.live_manager import LiveManager


def validate_download_link(download_link: str) -> bool:
    """Check if a download link is accessible."""
    try:
        response = requests.head(download_link, headers=DOWNLOAD_HEADERS, timeout=5)
        if response.status_code == 503:
            log_http_503(
                route="HEAD",
                url=getattr(response, "url", None) or download_link,
                filename="link validation",
                retry=1,
                maximum=1,
                retry_after=response.headers.get("Retry-After"),
                wait=0,
                fresh_signed_url=False,
                reason="validation request does not retry",
            )

    except requests.RequestException:
        return False

    return response.status_code != HTTPStatus.SERVER_DOWN


async def fetch_page(url: str, retries: int = 5) -> BeautifulSoup | None:
    """Fetch the HTML content of a page at the given URL, with retry logic."""
    tried_fallback = False

    def handle_response(response: Response) -> BeautifulSoup | None:
        """Process the HTTP response and handles specific status codes."""
        if response.status_code in FETCH_ERROR_MESSAGES:
            log_message = FETCH_ERROR_MESSAGES[response.status_code].format(url=url)
            logging.exception(log_message)
            return None

        # Use raw bytes to let BS4 detect encoding
        soup = BeautifulSoup(response.content, "html.parser")
        title = soup.title.get_text(" ", strip=True).lower() if soup.title else ""
        body_text = soup.get_text(" ", strip=True).lower()[:2000]
        maintenance_phrases = (
            "under maintenance",
            "site maintenance",
            "temporarily unavailable",
            "we'll be back soon",
        )
        if any(phrase in title or phrase in body_text for phrase in maintenance_phrases):
            logging.warning("Bunkr returned a maintenance page for %s", url)
            return None
        return soup

    with requests.Session() as session:
        for attempt in range(retries):
            response = None
            status = None
            try:
                response = session.get(
                    url, headers=DOWNLOAD_HEADERS, timeout=40, allow_redirects=True,
                )
                if response.status_code == HTTPStatus.FORBIDDEN and not tried_fallback:
                    tried_fallback = True
                    url = replace_domain_with_fallback(url)
                    continue  # Retry immediately with .cr

                response.raise_for_status()
                return handle_response(response)

            except RemoteDisconnected:
                logging.warning("Remote end closed connection without response.")
            except requests.RequestException as err:
                response = getattr(err, "response", None) or response
                status = response.status_code if response is not None else None
                if status not in (429, 500, 502, 503, 504) and response is not None:
                    logging.warning("Page request failed with HTTP %s for %s", status, url)
                    return None
                if status is None and not isinstance(
                    err, (requests.Timeout, requests.ConnectionError),
                ):
                    logging.warning("Page request failed: %s", err)
                    return None
            finally:
                if response is not None:
                    response.close()

            if attempt < retries - 1:
                delay = min(2 ** (attempt + 1), 30) + random.uniform(0.25, 1.0)  # noqa: S311
                if status == 503:
                    log_http_503(
                        route="PAGE_GET",
                        url=getattr(response, "url", None) or url,
                        filename="page resolve",
                        retry=attempt + 1,
                        maximum=retries,
                        retry_after=response.headers.get("Retry-After"),
                        wait=delay,
                        fresh_signed_url=False,
                        phase="PAGE",
                    )
                await asyncio.sleep(delay)
            elif status == 503:
                log_http_503(
                    route="PAGE_GET",
                    url=getattr(response, "url", None) or url,
                    filename="page resolve",
                    retry=retries,
                    maximum=retries,
                    retry_after=response.headers.get("Retry-After"),
                    wait=0,
                    fresh_signed_url=False,
                    final=True,
                    phase="PAGE",
                    reason="page retry budget exhausted",
                )

    return None


def clear_terminal() -> None:
    """Clear the terminal screen based on the operating system."""
    commands = {
        "nt": "cls",       # Windows
        "posix": "clear",  # macOS and Linux
    }

    command = commands.get(os.name)
    if command:
        os.system(command)  # noqa: S605


def check_python_version(min_version: tuple[int, int] = (3, 10)) -> None:
    """Check if the current Python version meets the minimum requirement."""
    current_version = sys.version_info
    if current_version < min_version:
        log_message = (
            f"Python {current_version.major}.{current_version.minor} is not supported. "
            f" Python {min_version[0]}.{min_version[1]} or higher is required.",
        )
        logging.warning(log_message)
        sys.exit(1)


def get_root_path() -> str:
    """Return the filesystem root for the current working directory."""
    cwd = Path.cwd()
    if platform.system() == "Windows":
        return os.path.splitdrive(cwd)[0] + "\\"

    # Use actual working directory
    return cwd


def check_disk_space(live_manager: LiveManager, custom_path: str | None = None) -> None:
    """Check if the available disk space is greater than or equal to `min_space` GB."""
    root_path = get_root_path() if custom_path is None else custom_path
    _, _, free_space = shutil.disk_usage(root_path)
    free_space_gb = free_space / GB

    if free_space_gb < MIN_DISK_SPACE_GB:
        live_manager.update_log(
            event="Insufficient disk space",
            details=f"Only {free_space_gb:.2f} GB available on {root_path}. "
            "The program has been stopped to prevent data loss.",
        )
        sys.exit(1)
