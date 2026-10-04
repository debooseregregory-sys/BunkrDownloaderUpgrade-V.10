"""Targeted regression tests for Bunkr IDs, resolve failures, and token refresh."""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import requests
from bs4 import BeautifulSoup

from src.config import DownloadInfo, RetryConfig, SessionInfo
from src.crawlers.api_utils import (
    ResolveFailure,
    extract_file_id,
    get_api_response,
    get_download_response,
)
from src.downloaders.media_downloader import MediaDownloader


class _AsyncResponse:
    def __init__(self, payload=None, status=200, headers=None):
        self.payload = payload or {}
        self.status = status
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def json(self):
        return self.payload


class _AsyncSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class BunkrResolutionTests(unittest.TestCase):
    def test_file_id_from_first_script(self):
        soup = BeautifulSoup('<script data-file-id="12345"></script>', "html.parser")
        self.assertEqual(extract_file_id(soup), "12345")

    def test_file_id_from_later_script(self):
        soup = BeautifulSoup(
            '<script>unrelated</script><script>const fileId = "23456";</script>',
            "html.parser",
        )
        self.assertEqual(extract_file_id(soup), "23456")

    def test_file_id_from_relevant_download_link(self):
        soup = BeautifulSoup('<a id="download-btn" data-id="34567" href="#">Download</a>', "html.parser")
        self.assertEqual(extract_file_id(soup), "34567")

    def test_missing_or_non_numeric_id_is_rejected(self):
        self.assertIsNone(extract_file_id(BeautifulSoup("<html></html>", "html.parser")))
        soup = BeautifulSoup(
            '<script data-file-id="not-a-number"></script><div data-file-id="12x"></div>',
            "html.parser",
        )
        self.assertIsNone(extract_file_id(soup))

    def test_invalid_candidate_does_not_hide_a_later_valid_id(self):
        soup = BeautifulSoup(
            '<script data-file-id="bad"></script><script data-file-id="45678"></script>',
            "html.parser",
        )
        self.assertEqual(extract_file_id(soup), "45678")

    def test_signed_url_preserves_cdn_path_and_existing_query(self):
        session = _AsyncSession([_AsyncResponse({"token": "secret", "ex": 99})])
        soup = BeautifulSoup(
            '<script>var jsCDN = "https://cdn.test/media/movie.mkv?download=1";</script>',
            "html.parser",
        )
        url = asyncio.run(get_api_response(session, "https://bunkr.test/f/1", soup))
        self.assertTrue(url.startswith("https://cdn.test/media/movie.mkv?"))
        self.assertIn("download=1", url)
        self.assertIn("token=secret", url)
        self.assertEqual(session.calls[0][1]["params"], {"path": "/media/movie.mkv?download=1"})

    def test_api_meta_path_and_query_are_preserved(self):
        class Session:
            def post(self, *_args, **_kwargs):
                return _AsyncResponse({
                    "mediafiles": "https://cdn.test/base?edge=west",
                    "path": "/storage/media/renamed.mkv?variant=2",
                })

        url = asyncio.run(get_download_response(Session(), "123"))
        self.assertEqual(
            url,
            "https://cdn.test/storage/media/renamed.mkv?edge=west&variant=2",
        )

    def test_insufficient_api_payload_fails_explicitly(self):
        class Session:
            def post(self, *_args, **_kwargs):
                return _AsyncResponse({"mediafiles": "https://cdn.test"})

        with self.assertRaisesRegex(ResolveFailure, "mediafiles/path is missing"):
            asyncio.run(get_download_response(Session(), "123"))

    def test_missing_token_fails_closed(self):
        session = _AsyncSession([_AsyncResponse({"ex": 99})])
        soup = BeautifulSoup('<script>var jsCDN = "https://cdn.test/video.mp4";</script>', "html.parser")
        with self.assertRaisesRegex(ResolveFailure, "TOKEN_FAILED"):
            asyncio.run(get_api_response(session, "https://bunkr.test/f/1", soup))

    def test_signing_429_honors_retry_after_then_resolves(self):
        session = _AsyncSession([
            _AsyncResponse(status=429, headers={"Retry-After": "17"}),
            _AsyncResponse({"token": "fresh", "ex": 99}),
        ])
        soup = BeautifulSoup('<script>var jsCDN = "https://cdn.test/video.mp4";</script>', "html.parser")
        with patch("src.crawlers.api_utils.asyncio.sleep", new_callable=AsyncMock) as sleep:
            url = asyncio.run(get_api_response(session, "https://bunkr.test/f/1", soup))
        self.assertIn("token=fresh", url)
        sleep.assert_awaited_once_with(17.0)

    def test_signing_temporary_500_retries_but_404_does_not(self):
        soup = BeautifulSoup('<script>var jsCDN = "https://cdn.test/video.mp4";</script>', "html.parser")
        session = _AsyncSession([
            _AsyncResponse(status=503),
            _AsyncResponse({"token": "ok", "ex": 1}),
        ])
        with patch("src.crawlers.api_utils.asyncio.sleep", new_callable=AsyncMock) as sleep:
            self.assertIn("token=ok", asyncio.run(get_api_response(session, "u", soup)))
        self.assertEqual(len(session.calls), 2)
        sleep.assert_awaited_once()

        missing_session = _AsyncSession([_AsyncResponse(status=404)])
        with self.assertRaises(ResolveFailure) as caught:
            asyncio.run(get_api_response(missing_session, "u", soup))
        self.assertEqual(caught.exception.code, "RESOLVE_FAILED")
        self.assertEqual(len(missing_session.calls), 1)

    def test_network_error_is_retried_during_resolution(self):
        session = _AsyncSession([
            asyncio.TimeoutError(),
            _AsyncResponse({"token": "ok", "ex": 1}),
        ])
        soup = BeautifulSoup('<script>var jsCDN = "https://cdn.test/video.mp4";</script>', "html.parser")
        with patch("src.crawlers.api_utils.asyncio.sleep", new_callable=AsyncMock):
            self.assertIn("token=ok", asyncio.run(get_api_response(session, "u", soup)))
        self.assertEqual(len(session.calls), 2)

    def test_401_and_403_trigger_fresh_resolve_before_retry(self):
        for status in (401, 403):
            with self.subTest(status=status):
                args = MagicMock(connections=1, ignore=[], include=[])
                info = DownloadInfo("https://b/f/1", "https://cdn/old?token=expired", "v.mp4", 1)
                downloader = MediaDownloader(
                    SessionInfo(args=args, bunkr_status={}, download_path="."),
                    info,
                    MagicMock(),
                    RetryConfig(retries=2),
                    refresh_download_link=MagicMock(return_value="https://cdn/new?token=fresh"),
                )
                first = requests.Response()
                first.status_code = status
                first.raw = MagicMock()
                first_error = requests.HTTPError(response=first)
                good_response = MagicMock()
                good_response.raise_for_status.return_value = None
                with patch("src.downloaders.media_downloader.detect_range_support", return_value=(False, -1)), \
                     patch("src.downloaders.media_downloader.requests.get", side_effect=[first_error, good_response]) as get, \
                     patch("src.downloaders.media_downloader.save_file_with_progress", return_value=False), \
                     patch("src.downloaders.media_downloader.time.sleep"):
                    self.assertFalse(downloader.attempt_download("out.mp4"))
                self.assertEqual(get.call_args_list[0].args[0], "https://cdn/old?token=expired")
                self.assertEqual(get.call_args_list[1].args[0], "https://cdn/new?token=fresh")

    def test_head_403_falls_back_to_get_before_resolving_again(self):
        args = MagicMock(connections=1, ignore=[], include=[])
        info = DownloadInfo("https://b/f/1", "https://cdn/file?token=current", "v.mp4", 1)
        refresh = MagicMock(return_value="https://cdn/file?token=fresh")
        downloader = MediaDownloader(
            SessionInfo(args=args, bunkr_status={}, download_path="."),
            info,
            MagicMock(),
            RetryConfig(retries=2),
            refresh_download_link=refresh,
        )
        head_response = requests.Response()
        head_response.status_code = 403
        good_response = MagicMock()
        good_response.raise_for_status.return_value = None
        with patch("src.downloaders.download_utils.requests.head", return_value=head_response), \
             patch("src.downloaders.media_downloader.requests.get", return_value=good_response) as get, \
             patch("src.downloaders.media_downloader.save_file_with_progress", return_value=False):
            self.assertFalse(downloader.attempt_download("out.mp4"))
        refresh.assert_not_called()
        get.assert_called_once()
        self.assertEqual(get.call_args.args[0], "https://cdn/file?token=current")

    def test_head_503_remains_diagnostic_and_get_fallback_works(self):
        args = MagicMock(connections=1, ignore=[], include=[])
        info = DownloadInfo("https://b/f/1", "https://cdn/file?token=current", "v.mp4", 1)
        refresh = MagicMock(return_value="https://cdn/file?token=fresh")
        downloader = MediaDownloader(
            SessionInfo(args=args, bunkr_status={}, download_path="."),
            info,
            MagicMock(),
            RetryConfig(retries=2),
            refresh_download_link=refresh,
        )
        head_response = requests.Response()
        head_response.status_code = 503
        head_response.headers["Retry-After"] = "40"
        head_error = requests.HTTPError(response=head_response)
        good_response = MagicMock()
        good_response.raise_for_status.return_value = None
        with patch("src.downloaders.download_utils.requests.head", side_effect=head_error), \
             patch("src.downloaders.media_downloader.requests.get", return_value=good_response) as get, \
             patch("src.downloaders.media_downloader.save_file_with_progress", return_value=False), \
             patch("src.downloaders.media_downloader.time.sleep") as sleep:
            self.assertFalse(downloader.attempt_download("out.mp4"))
        refresh.assert_not_called()
        sleep.assert_not_called()
        get.assert_called_once_with(
            "https://cdn/file?token=current",
            stream=True,
            headers=unittest.mock.ANY,
            timeout=30,
            allow_redirects=True,
        )

    def test_media_503_waits_then_resolves_fresh_url_before_get(self):
        args = MagicMock(connections=1, ignore=[], include=[])
        info = DownloadInfo("https://b/f/1", "https://cdn/old?token=old", "v.mp4", 1)
        refresh = MagicMock(return_value="https://cdn/new?token=new")
        downloader = MediaDownloader(
            SessionInfo(args=args, bunkr_status={}, download_path="."),
            info,
            MagicMock(),
            RetryConfig(retries=2),
            refresh_download_link=refresh,
        )
        response = requests.Response()
        response.status_code = 503
        response.headers["Retry-After"] = "23"
        response.raw = MagicMock()
        error = requests.HTTPError(response=response)
        good_response = MagicMock()
        good_response.raise_for_status.return_value = None
        with self.assertLogs(level="INFO") as output:
            with patch("src.downloaders.media_downloader.detect_range_support", return_value=(False, -1)), \
                 patch("src.downloaders.media_downloader.requests.get", side_effect=[error, good_response]) as get, \
                 patch("src.downloaders.media_downloader.save_file_with_progress", return_value=False), \
                 patch("src.downloaders.media_downloader.time.sleep") as sleep, \
                 patch("src.downloaders.media_downloader.log_http_503") as log_503:
                self.assertFalse(downloader.attempt_download("out.mp4"))
        self.assertEqual([call.args[0] for call in get.call_args_list], [
            "https://cdn/old?token=old", "https://cdn/new?token=new",
        ])
        refresh.assert_called_once_with()
        sleep.assert_called_once_with(23.0)
        self.assertEqual(log_503.call_args.kwargs["fresh_signed_url"], False)
        joined_logs = "\n".join(output.output)
        self.assertIn("fresh signed URL obtained", joined_logs)
        self.assertIn("fresh_signed_url=true", joined_logs)
        self.assertNotIn("token=old", joined_logs)
        self.assertNotIn("token=new", joined_logs)

    def test_media_503_without_retry_after_uses_long_jittered_backoff(self):
        downloader = MediaDownloader(
            SessionInfo(args=MagicMock(), bunkr_status={}, download_path="."),
            DownloadInfo("item", "https://cdn/v?token=x", "v.mp4", 1),
            MagicMock(),
            RetryConfig(retries=3),
        )
        response = requests.Response()
        response.status_code = 503
        response.raw = MagicMock()
        error = requests.HTTPError(response=response)
        with patch("src.downloaders.media_downloader.random.uniform", return_value=4.0) as jitter, \
             patch("src.downloaders.media_downloader.time.sleep") as sleep:
            self.assertTrue(downloader._handle_request_exception(error, 0))
        jitter.assert_called_once_with(2.0, 5.0)
        sleep.assert_called_once_with(19.0)

    def test_chunk_503_skips_chunk_retry_and_fresh_resolves(self):
        args = MagicMock(connections=4, ignore=[], include=[])
        info = DownloadInfo("https://b/f/1", "https://cdn/old?token=old", "v.mp4", 1)
        refresh = MagicMock(return_value="https://cdn/new?token=new")
        downloader = MediaDownloader(
            SessionInfo(args=args, bunkr_status={}, download_path="."),
            info,
            MagicMock(),
            RetryConfig(retries=2),
            refresh_download_link=refresh,
        )
        response = requests.Response()
        response.status_code = 503
        response.raw = MagicMock()
        error = requests.HTTPError(response=response)
        with patch("src.downloaders.media_downloader.detect_range_support", return_value=(True, 50_000_000)), \
             patch("src.downloaders.media_downloader.save_file_with_chunks", side_effect=[error, False]) as chunks, \
             patch("src.downloaders.media_downloader.time.sleep") as sleep:
            self.assertFalse(downloader.attempt_download("out.mp4"))
        self.assertEqual(
            [call.args[0] for call in chunks.call_args_list],
            ["https://cdn/old?token=old", "https://cdn/new?token=new"],
        )
        refresh.assert_called_once_with()
        sleep.assert_called_once()

    def test_429_retry_after_is_capped(self):
        downloader = MediaDownloader(
            SessionInfo(args=MagicMock(), bunkr_status={}, download_path="."),
            DownloadInfo("u", "old", "v.mp4", 1),
            MagicMock(),
            RetryConfig(retries=2),
        )
        response = requests.Response()
        response.status_code = 429
        response.headers["Retry-After"] = "900"
        error = requests.HTTPError(response=response)
        with patch("src.downloaders.media_downloader.time.sleep") as sleep:
            self.assertTrue(downloader._handle_request_exception(error, 0))
        sleep.assert_called_once_with(120.0)


if __name__ == "__main__":
    unittest.main()
