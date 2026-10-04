"""Regression tests for edition branding and opt-in shutdown behavior."""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import gui
from src.shutdown_manager import (
    DEFAULT_SHUTDOWN_ENABLED,
    SHUTDOWN_COUNTDOWN_SECONDS,
    ShutdownManager,
    queue_completion_state,
)


class _FakeRoot:
    def __init__(self):
        self.next_id = 0
        self.callbacks = {}
        self.cancelled = []

    def after(self, milliseconds, callback):
        self.next_id += 1
        self.callbacks[self.next_id] = (milliseconds, callback)
        return self.next_id

    def after_cancel(self, timer_id):
        self.cancelled.append(timer_id)
        self.callbacks.pop(timer_id, None)


class GuiCustomizationTests(unittest.TestCase):
    def test_about_page_uses_kid_acid_edition_and_professional_copy(self):
        tab = MagicMock()
        labels = [MagicMock() for _ in range(6)]
        buttons = [MagicMock()]
        with patch("gui.ctk.CTkLabel", side_effect=labels) as make_label, \
             patch("gui.ctk.CTkButton", side_effect=buttons), \
             patch("gui.ctk.CTkFont"):
            gui.DownloaderUI._build_about_tab(SimpleNamespace(tab_about=tab))

        texts = [entry.kwargs.get("text") for entry in make_label.call_args_list]
        self.assertIn("Bunkr Downloader", texts)
        self.assertIn("Kid Acid Edition · V. 1.0.0", texts)
        self.assertIn(gui.EDITION_DESCRIPTION, texts)
        self.assertIn(gui.ABOUT_TROUBLESHOOTING, texts)
        self.assertIn("Backend v" + gui.BACKEND_VERSION, texts)
        self.assertNotIn("ZeroHackz", "\n".join(str(text) for text in texts))

    def test_github_button_opens_exact_new_project_and_not_old_repo(self):
        tab = MagicMock()
        button = MagicMock()
        with patch("gui.ctk.CTkLabel"), patch("gui.ctk.CTkButton", return_value=button), \
             patch("gui.ctk.CTkFont"), patch("gui.webbrowser.open") as open_url:
            make_button = gui.ctk.CTkButton
            gui.DownloaderUI._build_about_tab(SimpleNamespace(tab_about=tab))
            make_button.call_args.kwargs["command"]()

        self.assertEqual(
            gui.GITHUB_URL,
            "https://github.com/debooseregregory-sys/BunkrDownloaderUpgrade-V.10",
        )
        open_url.assert_called_once_with(gui.GITHUB_URL)
        self.assertNotIn("ZeroHackz", gui.GITHUB_URL)

    def test_home_heading_displays_kid_acid_branding(self):
        heading = MagicMock()
        labels = [MagicMock(), MagicMock()]
        with patch("gui.ctk.CTkFrame", return_value=heading), \
             patch("gui.ctk.CTkLabel", side_effect=labels) as make_label, \
             patch("gui.ctk.CTkFont"):
            gui.DownloaderUI._build_home_heading(SimpleNamespace(), MagicMock())
        texts = [entry.kwargs.get("text") for entry in make_label.call_args_list]
        self.assertIn("Bunkr Downloader", texts)
        self.assertIn("UPGRADED BY KID ACID", texts)

    def test_shutdown_checkbox_is_opt_in_by_default(self):
        self.assertFalse(DEFAULT_SHUTDOWN_ENABLED)
        card, notice = MagicMock(), MagicMock()
        checkbox = MagicMock()
        fake_ui = SimpleNamespace(
            _on_shutdown_option_changed=MagicMock(),
            _cancel_shutdown=MagicMock(),
        )
        with patch("gui.ctk.CTkFrame", side_effect=[card, notice]), \
             patch("gui.ctk.BooleanVar") as boolean_var, \
             patch("gui.ctk.CTkCheckBox", return_value=checkbox) as make_checkbox, \
             patch("gui.ctk.CTkLabel"), patch("gui.ctk.CTkButton"), \
             patch("gui.ctk.CTkFont"):
            gui.DownloaderUI._build_shutdown_controls(fake_ui, MagicMock())
        boolean_var.assert_called_once_with(value=False)
        self.assertEqual(
            make_checkbox.call_args.kwargs["text"],
            "PC afsluiten wanneer alle downloads voltooid zijn",
        )

    def test_enabling_checkbox_shows_active_status(self):
        status = MagicMock()
        ui = SimpleNamespace(
            auto_shutdown_var=MagicMock(get=MagicMock(return_value=True)),
            shutdown_option_status=status,
        )
        with patch("gui.messagebox.askyesno", return_value=True):
            gui.DownloaderUI._on_shutdown_option_changed(ui)
        status.grid.assert_called_once()

    def test_unchecking_shutdown_option_cancels_pending_countdown(self):
        manager = MagicMock(pending=True)
        ui = SimpleNamespace(
            auto_shutdown_var=MagicMock(get=MagicMock(return_value=False)),
            shutdown_manager=manager,
            shutdown_option_status=MagicMock(),
            _cancel_shutdown=MagicMock(),
        )
        gui.DownloaderUI._on_shutdown_option_changed(ui)
        ui._cancel_shutdown.assert_called_once_with()

    def test_shutdown_gate_waits_for_all_items_and_worker_to_finish(self):
        ui = SimpleNamespace(
            auto_shutdown_var=MagicMock(get=MagicMock(return_value=True)),
            _batch_active=True,
            _worker_thread=None,
            after=MagicMock(),
            _queue_items=[{"status": "Completed"}, {"status": "Downloading"}],
            _stop_requested=False,
            _active_proc=None,
            shutdown_manager=MagicMock(),
        )
        ui._consider_auto_shutdown = lambda: gui.DownloaderUI._consider_auto_shutdown(ui)
        gui.DownloaderUI._consider_auto_shutdown(ui)
        ui.after.assert_called_once_with(100, ui._consider_auto_shutdown)
        ui.shutdown_manager.schedule.assert_not_called()

    def test_shutdown_is_scheduled_only_after_all_queue_items_are_done(self):
        manager = MagicMock()
        ui = SimpleNamespace(
            auto_shutdown_var=MagicMock(get=MagicMock(return_value=True)),
            _batch_active=False,
            _worker_thread=None,
            _queue_items=[{"status": "Completed"}, {"status": "Skipped"}],
            _stop_requested=False,
            _active_proc=None,
            shutdown_manager=manager,
            status_var=MagicMock(),
        )
        with patch("gui.messagebox.askyesno") as confirm:
            gui.DownloaderUI._consider_auto_shutdown(ui)
        manager.schedule.assert_called_once_with()
        confirm.assert_not_called()

    def test_completed_queue_shows_gui_countdown_and_cancel_hides_it(self):
        root = _FakeRoot()
        runner = MagicMock()
        notice_label = MagicMock()
        notice = MagicMock()
        shutdown_var = MagicMock(get=MagicMock(return_value=True))
        ui = SimpleNamespace(
            auto_shutdown_var=shutdown_var,
            _batch_active=False,
            _worker_thread=None,
            _queue_items=[{"status": "Completed"}, {"status": "Skipped"}],
            _stop_requested=False,
            _active_proc=None,
            shutdown_notice=notice,
            shutdown_notice_label=notice_label,
            shutdown_option_status=MagicMock(),
            status_var=MagicMock(),
        )
        ui.shutdown_manager = ShutdownManager(
            root,
            lambda remaining: gui.DownloaderUI._update_shutdown_notice(ui, remaining),
            command_runner=runner,
            platform_name="nt",
        )
        ui._cancel_shutdown = lambda: gui.DownloaderUI._cancel_shutdown(ui)

        gui.DownloaderUI._consider_auto_shutdown(ui)

        notice_label.configure.assert_called_once_with(
            text="All downloads completed. PC will shut down in 30 seconds.",
        )
        notice.grid.assert_called_once_with(row=2, column=0, columnspan=2, sticky="ew")
        self.assertEqual(
            runner.call_args_list[0].args[0][:4],
            ["shutdown.exe", "/s", "/t", "30"],
        )

        gui.DownloaderUI._cancel_shutdown(ui)

        self.assertEqual(runner.call_args_list[1].args[0], ["shutdown.exe", "/a"])
        notice.grid_remove.assert_called_once()
        shutdown_var.set.assert_called_once_with(False)

    def test_skipped_items_are_complete_for_shutdown(self):
        self.assertEqual(
            queue_completion_state(
                ["Skipped", "Skipped · already exists", "Completed"],
                batch_active=False, stop_requested=False, active_process=False,
            ),
            "ready",
        )

    def test_failed_queue_requires_confirmation_before_shutdown(self):
        manager = MagicMock()
        ui = SimpleNamespace(
            auto_shutdown_var=MagicMock(get=MagicMock(return_value=True)),
            _batch_active=False,
            _worker_thread=None,
            _queue_items=[{"status": "Completed"}, {"status": "Failed"}],
            _stop_requested=False,
            _active_proc=None,
            shutdown_manager=manager,
            status_var=MagicMock(),
        )
        with patch("gui.messagebox.askyesno", return_value=False) as confirm:
            gui.DownloaderUI._consider_auto_shutdown(ui)
        confirm.assert_called_once()
        manager.schedule.assert_not_called()

    def test_unverified_external_results_require_confirmation(self):
        manager = MagicMock()
        ui = SimpleNamespace(
            auto_shutdown_var=MagicMock(get=MagicMock(return_value=True)),
            _batch_active=False,
            _worker_thread=None,
            _queue_items=[{"status": "Unverified"}],
            _stop_requested=False,
            _active_proc=None,
            shutdown_manager=manager,
            status_var=MagicMock(),
        )
        with patch("gui.messagebox.askyesno", return_value=False) as confirm:
            gui.DownloaderUI._consider_auto_shutdown(ui)
        confirm.assert_called_once()
        self.assertIn("external console", confirm.call_args.args[1])
        manager.schedule.assert_not_called()

    def test_shutdown_manager_uses_normal_countdown_and_can_cancel(self):
        root = _FakeRoot()
        runner = MagicMock()
        notices = []
        manager = ShutdownManager(
            root, notices.append, command_runner=runner, platform_name="nt",
        )

        self.assertTrue(manager.schedule())
        self.assertEqual(
            runner.call_args_list[0].args[0],
            ["shutdown.exe", "/s", "/t", str(SHUTDOWN_COUNTDOWN_SECONDS),
             "/c", "All queued downloads have finished."],
        )
        self.assertNotIn("/f", runner.call_args_list[0].args[0])
        self.assertEqual(notices, [SHUTDOWN_COUNTDOWN_SECONDS])
        timer_id = manager._timer_id

        self.assertTrue(manager.cancel())
        self.assertEqual(runner.call_args_list[1].args[0], ["shutdown.exe", "/a"])
        self.assertEqual(root.cancelled, [timer_id])
        self.assertEqual(notices[-1], None)
        self.assertFalse(manager.pending)

    def test_shutdown_manager_refuses_non_windows_and_does_not_run_command(self):
        root = _FakeRoot()
        runner = MagicMock()
        manager = ShutdownManager(
            root, MagicMock(), command_runner=runner, platform_name="posix",
        )
        self.assertFalse(manager.schedule())
        runner.assert_not_called()

    def test_queue_gate_treats_active_retry_waiting_and_failed_as_not_success(self):
        for status in ("Waiting", "Downloading", "Retrying"):
            with self.subTest(status=status):
                self.assertEqual(
                    queue_completion_state(
                        [status], batch_active=False, stop_requested=False,
                        active_process=False,
                    ),
                    "pending",
                )
        self.assertEqual(
            queue_completion_state(
                ["Failed"], batch_active=False, stop_requested=False,
                active_process=False,
            ),
            "failed",
        )
        self.assertEqual(
            queue_completion_state(
                ["Unverified"], batch_active=False, stop_requested=False,
                active_process=False,
            ),
            "unverified",
        )


if __name__ == "__main__":
    unittest.main()
