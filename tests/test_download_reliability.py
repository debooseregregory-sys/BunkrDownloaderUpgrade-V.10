"""Offline regression tests for retry and safe-download behavior."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests

from src.config import (
    MAX_WORKERS,
    AlbumInfo,
    ChunkInfo,
    DownloadInfo,
    RetryConfig,
    SessionInfo,
    parse_arguments,
)
from src.crawlers.api_utils import get_api_response
from src.downloaders.album_downloader import AlbumDownloader
from src.downloaders.download_utils import _download_single_chunk, save_file_with_progress
from src.downloaders.media_downloader import MediaDownloader
from src.general_utils import fetch_page


class DownloadReliabilityTests(unittest.TestCase):
    def test_default_worker_count_is_one_and_explicit_value_parses(self):
        self.assertEqual(MAX_WORKERS, 1)
        self.assertEqual(AlbumDownloader.download_album.__defaults__[0], 1)
        with patch("src.config._find_config_file", return_value=None), \
             patch("sys.argv", ["downloader.py", "https://bunkr.cr/a/test"]):
            self.assertEqual(parse_arguments().max_workers, 1)
        with patch("src.config._find_config_file", return_value=None), \
             patch("sys.argv", ["downloader.py", "https://bunkr.cr/a/test", "--max-workers", "4"]):
            self.assertEqual(parse_arguments().max_workers, 4)

    def test_gui_worker_slider_is_passed_to_downloader_arguments(self):
        from gui import DownloaderUI

        ui = SimpleNamespace(
            dest_var=MagicMock(get=MagicMock(return_value="")),
            settings_include=MagicMock(get=MagicMock(return_value="")),
            settings_exclude=MagicMock(get=MagicMock(return_value="")),
            retries_slider=MagicMock(get=MagicMock(return_value=5)),
            workers_slider=MagicMock(get=MagicMock(return_value=4)),
            opt_no_disk_check=MagicMock(get=MagicMock(return_value=False)),
            opt_no_dl_folder=MagicMock(get=MagicMock(return_value=False)),
        )
        args = DownloaderUI._build_downloader_args(
            ui, "https://bunkr.cr/a/test", include_ui_flag=True,
        )
        self.assertEqual(args[args.index("--max-workers") + 1], "4")

    def test_explicit_workers_reach_album_downloader(self):
        from downloader import handle_download_process

        async def run_test():
            album_instance = MagicMock()
            album_instance.download_album = unittest.mock.AsyncMock(return_value=False)
            with patch("downloader.get_identifier", return_value="album"), \
                 patch("downloader.check_url_type", return_value=True), \
                 patch("downloader.get_item_pages_with_cache", new=unittest.mock.AsyncMock(
                     return_value=([], {}),
                 )), \
                 patch("downloader.AlbumDownloader", return_value=album_instance):
                result = await handle_download_process(
                    MagicMock(),
                    "https://bunkr.cr/a/test",
                    object(),
                    MagicMock(),
                    max_retries=5,
                    max_workers=4,
                )
            self.assertFalse(result)
            album_instance.download_album.assert_awaited_once_with(
                max_workers=4,
                max_retries=5,
            )

        asyncio.run(run_test())

    def test_missing_content_length_can_finish_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "clip.mp4"
            response = MagicMock()
            response.headers = {}
            response.iter_content.return_value = [b"video", b"data"]
            manager = MagicMock()

            outcome = save_file_with_progress(response, str(target), 1, manager)

            self.assertFalse(outcome)
            self.assertEqual(target.read_bytes(), b"videodata")
            self.assertTrue(response.close.called)

    def test_incomplete_body_is_not_published_as_a_final_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "clip.mp4"
            response = MagicMock()
            response.headers = {"Content-Length": "20"}
            response.iter_content.return_value = [b"short"]

            outcome = save_file_with_progress(response, str(target), 1, MagicMock())

            self.assertTrue(outcome)
            self.assertFalse(target.exists())

    def test_network_error_is_retryable_and_does_not_mark_host_offline(self):
        args = MagicMock()
        args.connections = 1
        args.ignore = []
        args.include = []
        session = SessionInfo(args=args, bunkr_status={}, download_path=".")
        info = DownloadInfo(item_url="https://example.test/f/1", download_link="https://cdn.test/v", filename="v.mp4", task=1)
        downloader = MediaDownloader(session, info, MagicMock(), RetryConfig(retries=2))
        with patch.object(downloader, "_retry_with_backoff", return_value=True) as retry:
            error = requests.ConnectionError("timeout")
            self.assertTrue(downloader._handle_request_exception(error, 0))
            retry.assert_called_once()
        self.assertEqual(session.bunkr_status, {})

    def test_chunk_503_is_delegated_without_fast_nested_chunk_retries(self):
        response = requests.Response()
        response.status_code = 503
        response.headers["Retry-After"] = "20"
        error = requests.HTTPError(response=response)
        with tempfile.TemporaryDirectory() as directory:
            chunk_path = Path(directory) / "chunk.part0"
            info = ChunkInfo(headers={}, on_progress=MagicMock())
            with patch("src.downloaders.download_utils.requests.get", side_effect=error) as get, \
                 patch("src.downloaders.download_utils.time.sleep") as sleep:
                with self.assertRaises(requests.HTTPError):
                    _download_single_chunk(
                        "https://cdn.test/video?token=sensitive",
                        (0, 15),
                        chunk_path,
                        info,
                    )
            get.assert_called_once()
            sleep.assert_not_called()

    def test_existing_file_is_skipped_without_overwrite_or_network(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "clip.mp4"
            target.write_bytes(b"existing video")
            args = MagicMock()
            args.connections = 1
            args.ignore = []
            args.include = []
            session = SessionInfo(args=args, bunkr_status={}, download_path=directory)
            info = DownloadInfo("https://b/f/1", "https://cdn/v", "clip.mp4", 1)
            manager = MagicMock()
            downloader = MediaDownloader(session, info, manager, RetryConfig(retries=2))
            with patch.object(downloader, "attempt_download") as attempt:
                self.assertFalse(downloader.download())
            attempt.assert_not_called()
            self.assertEqual(target.read_bytes(), b"existing video")
            manager.update_summary.assert_called_once()

    def test_same_url_rerun_skips_same_completed_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "clip.mp4").write_bytes(b"first successful download")
            args = MagicMock()
            args.connections = 1
            args.ignore = []
            args.include = []
            info = DownloadInfo("https://b/f/same", "https://cdn/v", "clip.mp4", 1)
            downloader = MediaDownloader(
                SessionInfo(args=args, bunkr_status={}, download_path=directory),
                info,
                MagicMock(),
                RetryConfig(retries=1),
            )
            with patch.object(downloader, "attempt_download") as attempt:
                self.assertFalse(downloader.download())
                self.assertFalse(downloader.download())
            attempt.assert_not_called()

    def test_stale_partial_temp_is_replaced_by_verified_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "clip.mp4"
            Path(f"{target}.bunkr-part").write_bytes(b"old partial")
            response = MagicMock()
            response.headers = {"Content-Length": "4"}
            response.iter_content.return_value = [b"good"]
            outcome = save_file_with_progress(response, str(target), 1, MagicMock())
            self.assertFalse(outcome)
            self.assertEqual(target.read_bytes(), b"good")
            self.assertFalse(Path(f"{target}.bunkr-part").exists())

    def test_zero_byte_destination_is_preserved_not_silently_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "clip.mp4"
            target.write_bytes(b"")
            args = MagicMock()
            args.connections = 1
            args.ignore = []
            args.include = []
            info = DownloadInfo("https://b/f/1", "https://cdn/v", "clip.mp4", 1)
            manager = MagicMock()
            downloader = MediaDownloader(
                SessionInfo(args=args, bunkr_status={}, download_path=directory),
                info, manager, RetryConfig(retries=1),
            )
            with patch.object(downloader, "attempt_download") as attempt:
                self.assertFalse(downloader.download())
            attempt.assert_not_called()
            self.assertEqual(target.read_bytes(), b"")
            self.assertTrue(any("preserved" in str(call).lower() for call in manager.update_log.call_args_list))

    def test_http_error_classification_keeps_429_and_5xx_retryable(self):
        args = MagicMock()
        session = SessionInfo(args=args, bunkr_status={}, download_path=".")
        info = DownloadInfo("https://b/f/1", "https://cdn/v", "clip.mp4", 1)
        downloader = MediaDownloader(session, info, MagicMock(), RetryConfig(retries=3))
        for status in (429, 500, 502, 503, 521):
            with self.subTest(status=status):
                error = requests.HTTPError("http error")
                error.response = MagicMock(status_code=status, headers={})
                with patch.object(downloader, "_retry_with_backoff", return_value=True) as retry:
                    self.assertTrue(downloader._handle_request_exception(error, 0))
                    retry.assert_called_once()

    def test_http_404_is_not_reported_as_offline_or_retried(self):
        session = SessionInfo(args=MagicMock(), bunkr_status={}, download_path=".")
        info = DownloadInfo("https://b/f/1", "https://cdn/v", "clip.mp4", 1)
        downloader = MediaDownloader(session, info, MagicMock(), RetryConfig(retries=3))
        error = requests.HTTPError("not found")
        error.response = MagicMock(status_code=404, headers={})
        with patch.object(downloader, "_retry_with_backoff") as retry:
            self.assertFalse(downloader._handle_request_exception(error, 0))
        retry.assert_not_called()

    def test_http_403_is_reported_without_marking_a_host_offline(self):
        session = SessionInfo(args=MagicMock(), bunkr_status={}, download_path=".")
        info = DownloadInfo("https://b/f/1", "https://cdn/v", "clip.mp4", 1)
        manager = MagicMock()
        downloader = MediaDownloader(session, info, manager, RetryConfig(retries=3))
        error = requests.HTTPError("forbidden")
        error.response = MagicMock(status_code=403, headers={})
        with patch.object(downloader, "_retry_with_backoff") as retry:
            self.assertFalse(downloader._handle_request_exception(error, 0))
        retry.assert_not_called()
        self.assertEqual(session.bunkr_status, {})

    def test_media_request_follows_redirects(self):
        args = MagicMock()
        args.connections = 1
        args.ignore = []
        args.include = []
        session = SessionInfo(args=args, bunkr_status={}, download_path=".")
        info = DownloadInfo("https://b/f/1", "https://cdn/v", "clip.mp4", 1)
        downloader = MediaDownloader(session, info, MagicMock(), RetryConfig(retries=1))
        response = MagicMock()
        response.raise_for_status.return_value = None
        with patch("src.downloaders.media_downloader.detect_range_support", return_value=(False, -1)), \
             patch("src.downloaders.media_downloader.requests.get", return_value=response) as get, \
             patch("src.downloaders.media_downloader.save_file_with_progress", return_value=False):
            self.assertFalse(downloader.attempt_download("clip.mp4"))
        self.assertTrue(get.call_args.kwargs["allow_redirects"])

    def test_album_cache_skips_existing_nonempty_items(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as directory:
                Path(directory, "cached.mp4").write_bytes(b"video")
                manager = MagicMock()
                manager.add_task.return_value = 4
                session = SessionInfo(args=MagicMock(), bunkr_status={}, download_path=directory)
                downloader = AlbumDownloader(
                    session,
                    AlbumInfo(album_id="album", item_pages=["https://b/f/1"]),
                    manager,
                    cached_items={"https://b/f/1": {"status": "completed", "filename": "cached.mp4"}},
                )
                with patch.object(downloader, "_fetch_page_with_retries") as fetch, \
                     patch("src.downloaders.album_downloader.get_download_info") as resolve:
                    await downloader.execute_item_download("https://b/f/1", 0, asyncio.Semaphore(1), 1)
                fetch.assert_not_called()
                resolve.assert_not_called()
                manager.update_summary.assert_called_once()
        asyncio.run(run_test())

    def test_album_with_eighty_cached_files_processes_only_remaining_twenty(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as directory:
                pages = [f"https://b/f/{index}" for index in range(100)]
                cached = {}
                for index in range(80):
                    filename = f"cached-{index}.mp4"
                    Path(directory, filename).write_bytes(b"video")
                    cached[pages[index]] = {"status": "completed", "filename": filename}
                manager = MagicMock()
                manager.add_task.return_value = 1
                session = SessionInfo(args=MagicMock(), bunkr_status={}, download_path=directory)
                downloader = AlbumDownloader(session, AlbumInfo("album", pages), manager, cached)
                with patch.object(downloader, "_fetch_page_with_retries", return_value=object()), \
                     patch("src.downloaders.album_downloader.get_item_filename", return_value="new.mp4"), \
                     patch("src.downloaders.album_downloader.get_download_info", return_value=("https://cdn.test/v", "new.mp4")), \
                     patch("src.downloaders.album_downloader.MediaDownloader.download", return_value=False) as download:
                    result = await downloader.download_album(max_workers=5, max_retries=1)
                self.assertFalse(result)
                self.assertEqual(download.call_count, 20)
        asyncio.run(run_test())

    def test_album_skips_existing_files_before_resolving_missing_items(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as directory:
                pages = [f"https://b/f/{index}" for index in range(100)]
                for index in range(73):
                    Path(directory, f"existing-{index}.mp4").write_bytes(b"video")
                manager = MagicMock()
                manager.add_task.return_value = 1
                session = SessionInfo(args=MagicMock(), bunkr_status={}, download_path=directory)
                downloader = AlbumDownloader(session, AlbumInfo("album", pages), manager)
                with patch.object(downloader, "_fetch_page_with_retries", return_value=object()), \
                     patch("src.downloaders.album_downloader.get_item_filename", side_effect=[
                         *[f"existing-{index}.mp4" for index in range(73)],
                         *[f"missing-{index}.mp4" for index in range(27)],
                     ]), \
                     patch("src.downloaders.album_downloader.get_download_info", return_value=("https://cdn.test/v", "missing.mp4")) as resolve, \
                     patch("src.downloaders.album_downloader.MediaDownloader.download", return_value=False) as download:
                    await downloader.download_album(max_workers=8, max_retries=1)
                self.assertEqual(resolve.call_count, 27)
                self.assertEqual(download.call_count, 27)
        asyncio.run(run_test())

    def test_fetch_page_retries_transient_connection_errors_with_headers(self):
        response = MagicMock()
        response.status_code = 200
        response.content = b"<html><title>ok</title></html>"
        response.raise_for_status.return_value = None
        session = MagicMock()
        session.__enter__.return_value = session
        session.get.side_effect = [requests.Timeout("temporary"), response]
        with patch("src.general_utils.requests.Session", return_value=session), \
             patch("src.general_utils.asyncio.sleep", new_callable=unittest.mock.AsyncMock):
            soup = asyncio.run(fetch_page("https://bunkr.test/f/x", retries=2))
        self.assertIsNotNone(soup)
        self.assertEqual(session.get.call_count, 2)
        self.assertIn("headers", session.get.call_args.kwargs)
        self.assertTrue(response.close.called)

    def test_http_404_page_does_not_retry(self):
        response = MagicMock(status_code=404)
        response.raise_for_status.side_effect = requests.HTTPError(response=response)
        session = MagicMock()
        session.__enter__.return_value = session
        session.get.return_value = response
        with patch("src.general_utils.requests.Session", return_value=session):
            soup = asyncio.run(fetch_page("https://bunkr.test/f/missing", retries=3))
        self.assertIsNone(soup)
        self.assertEqual(session.get.call_count, 1)

    def test_maintenance_page_is_identified_and_not_returned_as_content(self):
        response = MagicMock(status_code=200)
        response.content = b"<html><title>Under maintenance</title></html>"
        response.raise_for_status.return_value = None
        session = MagicMock()
        session.__enter__.return_value = session
        session.get.return_value = response
        with patch("src.general_utils.requests.Session", return_value=session):
            soup = asyncio.run(fetch_page("https://bunkr.test/", retries=1))
        self.assertIsNone(soup)

    def test_signing_api_retries_timeout_without_unbound_data_error(self):
        class AsyncResponse:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return None
            def raise_for_status(self):
                return None
            async def json(self):
                return {"token": "signed", "ex": "123"}

        class AsyncSession:
            def __init__(self):
                self.calls = 0
            def get(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise asyncio.TimeoutError()
                return AsyncResponse()

        async def run_test():
            session = AsyncSession()
            soup = BeautifulSoup(
                "<script>var jsCDN = 'https://cdn.test/video.mp4';</script>",
                "html.parser",
            )
            with patch("src.crawlers.api_utils.asyncio.sleep", new_callable=unittest.mock.AsyncMock):
                url = await get_api_response(session, "https://bunkr.test/f/a", soup)
            self.assertEqual(session.calls, 2)
            self.assertIn("token=signed", url)
        from bs4 import BeautifulSoup
        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
