"""
Graphical user interface for the Bunkr Downloader.
"""
#import asyncio
import io
import json
import logging
import os
import platform
import re
import sys
import threading
import webbrowser
from tkinter import filedialog, messagebox

import customtkinter as ctk

from src.config import DOWNLOAD_FOLDER, MAX_RETRIES, MAX_WORKERS
from src.shutdown_manager import (
    DEFAULT_SHUTDOWN_ENABLED,
    ShutdownManager,
    queue_completion_state,
)
from src.version import __version__ as BACKEND_VERSION

GUI_VERSION = "2026.08.03"
EDITION_TEXT = "Kid Acid Edition · V. 1.0.0"
EDITION_DESCRIPTION = (
    "An upgraded and modified edition of Bunkr Downloader focused on reliable "
    "downloads, safe file handling and clear progress tracking."
)
ABOUT_TROUBLESHOOTING = (
    "Troubleshooting: For HTTP 503 errors or unstable downloads, set "
    "Max concurrent downloads to 1."
)
HOME_BRANDING = "UPGRADED BY KID ACID"
GITHUB_URL = "https://github.com/debooseregregory-sys/BunkrDownloaderUpgrade-V.10"

# When running from a PyInstaller bundle, sys.executable is the GUI's own .exe
# and there's no separate python.exe to invoke — instead we self-exec with the
# `--gui-runner` sentinel to enter runner mode (see __main__ at the bottom).
IS_FROZEN = getattr(sys, "frozen", False)


def _child_python_exe():
    """Prefer pythonw.exe so the child has no console window. Only used when
    running from source — never returns the bundled .exe."""
    exe = sys.executable
    base = os.path.dirname(exe)
    candidate = os.path.join(base, "pythonw.exe")
    if os.path.isfile(candidate):
        return candidate
    return exe

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


def resource_path(relative_path):
    """Get absolute path to resource, works for dev and for PyInstaller."""
    try:
        base_path = sys._MEIPASS
    except AttributeError:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


class IORedirector(io.StringIO):
    """Thread-safe stdout redirector that writes to a CTkTextbox via after()."""

    def __init__(self, textbox, root):
        super().__init__()
        self.textbox = textbox
        self.root = root

    def _classify(self, text):
        """Map backend log lines to a color tag based on keywords."""
        lower = text.lower()
        if "download failed" in lower or "error" in lower or "exceeded" in lower:
            return "fail"
        if "skipped" in lower or "already been downloaded" in lower:
            return "skip"
        if "completed" in lower or "success" in lower:
            return "ok"
        return None

    def write(self, text):
        tag = self._classify(text)

        def _append():
            self.textbox.configure(state="normal")
            if tag:
                self.textbox.insert("end", text, tag)
            else:
                self.textbox.insert("end", text)
            self.textbox.see("end")
            self.textbox.configure(state="disabled")
        self.root.after(0, _append)

    def flush(self):
        pass


class DownloaderUI(ctk.CTk):
    """Main application window for the Bunkr Downloader."""

    def __init__(self):
        super().__init__()

        self.title("Bunkr Downloader")
        self.geometry("900x790")
        self.minsize(740, 620)
        self._stop_requested = False      # Pause: skip remaining URLs after current
        self._force_stop = False          # Stop: hard-kill current download
        self._worker_thread = None        # Worker thread running _run_batch
        self._active_proc = None          # External console subprocess (if used)
        self._album_total = 0             # Files in the currently downloading album
        self._album_done = 0              # Files finished so far in the album
        self._active_files = []           # Filenames currently downloading
        self._active_lock = threading.Lock()
        self._url_index = 0               # 1-based URL index in the current batch
        self._url_total = 0               # Total URLs in the current batch
        self._queue_items = []
        self._queue_sequence = 0
        self._queue_filter = ctk.StringVar(value="All")
        self._downloaded_count = 0
        self._skipped_count = 0
        self._failed_count = 0
        self._known_file_total = 0
        self._finished_file_total = 0
        self._batch_active = False

        try:
            icon_path = resource_path(os.path.join("misc", "gui", "icons", "icon.ico"))
            self.after(200, lambda: self.iconbitmap(icon_path))
        except Exception:
            pass

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # Shared StringVar for download destination (Download tab + Settings tab stay in sync).
        # Left blank by default so the backend uses its own './Downloads/' — pre-filling
        # this with "Downloads" would pass --custom-path Downloads, and the backend
        # would create Downloads/Downloads/<album>/.
        self.dest_var = ctk.StringVar(value="")

        # Tab view
        self.tabs = ctk.CTkTabview(self)
        self.tabs.grid(row=0, column=0, padx=16, pady=(16, 8), sticky="nsew")

        self.tab_dl = self.tabs.add("  Download  ")
        self.tab_activity = self.tabs.add("  Activity  ")
        self.tab_settings = self.tabs.add("  Settings  ")
        self.tab_about = self.tabs.add("  About  ")

        self._build_download_tab()
        self._build_activity_tab()
        self._build_settings_tab()
        self._build_about_tab()
        self.shutdown_manager = ShutdownManager(
            self, self._update_shutdown_notice,
        )

        # Must call after tabs are built (accesses self.mode)
        self._toggle_mode()

        # Status bar
        self.status_var = ctk.StringVar(value="Ready")
        ctk.CTkLabel(self, textvariable=self.status_var, anchor="w",
                     text_color="gray60").grid(
            row=1, column=0, padx=20, pady=(0, 10), sticky="ew")

        # Redirect stdout so downloader print() output flows into the log
        sys.stdout = IORedirector(self.log, self)

        # Route Python logging to the same textbox. The backend's log_manager calls
        # logging.info(...) when --disable-ui is on; without a handler, those messages
        # are silently swallowed and the GUI looks frozen during long downloads.
        gui_handler = logging.StreamHandler(sys.stdout)
        gui_handler.setFormatter(logging.Formatter("%(message)s"))
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        root_logger.addHandler(gui_handler)

    # ─────────────────────────────────────────────────────────────────────────
    # Tab builders
    # ─────────────────────────────────────────────────────────────────────────

    def _build_download_tab(self):
        tab = self.tab_dl
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(7, weight=1)

        self._build_home_heading(tab)

        mode_frame = ctk.CTkFrame(tab, fg_color="transparent")
        mode_frame.grid(row=1, column=0, padx=0, pady=(0, 4), sticky="ew")
        ctk.CTkLabel(mode_frame, text="Add from:", text_color="gray70").pack(
            side="left", padx=(4, 8))
        self.mode = ctk.StringVar(value="url")
        ctk.CTkRadioButton(mode_frame, text="Single URL",
                           variable=self.mode, value="url",
                           command=self._toggle_mode).pack(side="left", padx=6, pady=4)
        ctk.CTkRadioButton(mode_frame, text="Batch from file",
                           variable=self.mode, value="file",
                           command=self._toggle_mode).pack(side="left", padx=6, pady=4)

        self.url_frame = ctk.CTkFrame(tab)
        self.url_frame.grid_columnconfigure(0, weight=1)
        self.url_entry = ctk.CTkEntry(
            self.url_frame, height=38,
            placeholder_text="Paste a Bunkr album or file URL here…")
        self.url_entry.grid(row=0, column=0, padx=(10, 8), pady=10, sticky="ew")
        self.url_entry.bind("<Return>", lambda _event: self._add_current_input())
        ctk.CTkButton(self.url_frame, text="Add to queue", width=120, height=38,
                      command=self._add_current_input).grid(
            row=0, column=1, padx=(0, 10), pady=10)

        self.file_frame = ctk.CTkFrame(tab)
        self.file_frame.grid_columnconfigure(0, weight=1)
        self.file_entry = ctk.CTkEntry(
            self.file_frame, height=38,
            placeholder_text="Select a .txt file with one URL per line…")
        self.file_entry.grid(row=0, column=0, padx=(10, 8), pady=10, sticky="ew")
        ctk.CTkButton(self.file_frame, text="Browse", width=84, height=38,
                      command=self._browse_input_file).grid(
            row=0, column=1, padx=(0, 6), pady=10)
        ctk.CTkButton(self.file_frame, text="Add URLs", width=100, height=38,
                      command=self._add_current_input).grid(
            row=0, column=2, padx=(0, 10), pady=10)

        dest_frame = ctk.CTkFrame(tab)
        dest_frame.grid(row=3, column=0, padx=0, pady=(0, 9), sticky="ew")
        dest_frame.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(dest_frame, text="Save to", text_color="gray70").grid(
            row=0, column=0, padx=(12, 10), pady=10, sticky="w")
        self.dest_entry = ctk.CTkEntry(
            dest_frame, textvariable=self.dest_var,
            placeholder_text=f"Default: ./{DOWNLOAD_FOLDER}/")
        self.dest_entry.grid(row=0, column=1, padx=(0, 8), pady=8, sticky="ew")
        ctk.CTkButton(dest_frame, text="Change…", width=88,
                      command=self._browse_dest_dir).grid(
            row=0, column=2, padx=(0, 8), pady=8)
        ctk.CTkButton(dest_frame, text="Open", width=70,
                      fg_color="gray30", hover_color="gray40",
                      command=self._open_dest).grid(
            row=0, column=3, padx=(0, 10), pady=8)

        progress_card = ctk.CTkFrame(tab)
        progress_card.grid(row=4, column=0, padx=0, pady=(0, 9), sticky="ew")
        progress_card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(progress_card, text="Overall progress",
                     font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=0, column=0, padx=12, pady=(9, 2), sticky="w")
        self.progress = ctk.CTkProgressBar(progress_card, mode="determinate", height=10)
        self.progress.grid(row=1, column=0, padx=12, pady=(3, 6), sticky="ew")
        self.progress.set(0)
        self.total_progress_var = ctk.StringVar(value="0 of 0 files")
        ctk.CTkLabel(progress_card, textvariable=self.total_progress_var,
                     text_color="gray70").grid(
            row=2, column=0, padx=12, pady=(0, 3), sticky="w")
        self.counts_var = ctk.StringVar(value="Downloaded 0   ·   Skipped 0   ·   Failed 0")
        ctk.CTkLabel(progress_card, textvariable=self.counts_var,
                     text_color="gray70").grid(
            row=3, column=0, padx=12, pady=(0, 9), sticky="w")

        self._build_shutdown_controls(tab)

        queue_header = ctk.CTkFrame(tab, fg_color="transparent")
        queue_header.grid(row=6, column=0, padx=0, pady=(0, 4), sticky="ew")
        queue_header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(queue_header, text="Download queue",
                     font=ctk.CTkFont(size=16, weight="bold")).grid(
            row=0, column=0, padx=2, sticky="w")
        self.queue_count_var = ctk.StringVar(value="0 links")
        ctk.CTkLabel(queue_header, textvariable=self.queue_count_var,
                     text_color="gray60").grid(row=0, column=1, padx=(0, 10))
        self.filter_btn = ctk.CTkSegmentedButton(
            queue_header,
            values=["All", "Active", "Completed", "Skipped", "Failed", "Unverified"],
            variable=self._queue_filter,
            command=lambda _value: self._render_queue(),
            width=360,
        )
        self.filter_btn.grid(row=0, column=2, sticky="e")

        self.queue_list = ctk.CTkScrollableFrame(tab, corner_radius=8)
        self.queue_list.grid(row=7, column=0, padx=0, pady=(0, 8), sticky="nsew")

        active_card = ctk.CTkFrame(tab)
        active_card.grid(row=8, column=0, padx=0, pady=(0, 8), sticky="ew")
        active_card.grid_columnconfigure(0, weight=1)
        active_line = ctk.CTkFrame(active_card, fg_color="transparent")
        active_line.grid(row=0, column=0, padx=12, pady=(8, 0), sticky="ew")
        active_line.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(active_line, text="Now downloading",
                     text_color="gray70").grid(row=0, column=0, sticky="w")
        self.active_status_var = ctk.StringVar(value="Ready")
        ctk.CTkLabel(active_line, textvariable=self.active_status_var,
                     text_color="#93c5fd").grid(row=0, column=2, sticky="e")
        self.active_file_var = ctk.StringVar(value="Waiting for a download")
        ctk.CTkLabel(active_card, textvariable=self.active_file_var,
                     font=ctk.CTkFont(size=13, weight="bold"), anchor="w").grid(
            row=1, column=0, padx=12, pady=(4, 2), sticky="ew")
        self.active_progress = ctk.CTkProgressBar(active_card, height=8)
        self.active_progress.grid(row=2, column=0, padx=12, pady=(3, 10), sticky="ew")
        self.active_progress.set(0)

        btn_frame = ctk.CTkFrame(tab, fg_color="transparent")
        btn_frame.grid(row=9, column=0, padx=0, pady=(0, 2), sticky="ew")
        self.run_btn = ctk.CTkButton(
            btn_frame, text="▶  Start queue", height=40, width=150,
            font=ctk.CTkFont(size=14, weight="bold"), command=self._start_download)
        self.run_btn.pack(side="left", padx=(0, 8))
        self.pause_btn = ctk.CTkButton(
            btn_frame, text="Stop after current", height=40, width=150,
            fg_color="#b7791f", hover_color="#936015", state="disabled",
            command=self._pause)
        self.pause_btn.pack(side="left", padx=(0, 8))
        self.stop_btn = ctk.CTkButton(
            btn_frame, text="Cancel now", height=40, width=110,
            fg_color="#a93226", hover_color="#7b241c", state="disabled",
            command=self._stop)
        self.stop_btn.pack(side="left", padx=(0, 8))
        ctk.CTkButton(btn_frame, text="View activity", height=40, width=125,
                      fg_color="gray30", hover_color="gray40",
                      command=lambda: self.tabs.set("  Activity  ")).pack(side="right")
        ctk.CTkButton(btn_frame, text="Clear completed", height=40, width=128,
                      fg_color="gray30", hover_color="gray40",
                      command=self._clear_completed).pack(side="right", padx=(0, 6))
        ctk.CTkButton(btn_frame, text="Retry failed", height=40, width=105,
                      fg_color="gray30", hover_color="gray40",
                      command=self._retry_failed).pack(side="right", padx=(0, 6))
        self._render_queue()

    def _build_activity_tab(self):
        tab = self.tab_activity
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(tab, text="Activity and messages",
                     font=ctk.CTkFont(size=20, weight="bold")).grid(
            row=0, column=0, padx=2, pady=(2, 8), sticky="w")
        self.log = ctk.CTkTextbox(tab,
                                  font=ctk.CTkFont(family="Consolas", size=11),
                                  fg_color="#1a1a1a", text_color="#d4d4d4",
                                  wrap="word")
        self.log.grid(row=1, column=0, padx=0, pady=(0, 4), sticky="nsew")
        self.log.configure(state="disabled")

        # Color tags for the log textbox (uses underlying Tk Text widget)
        self.log.tag_config("header", foreground="#7dd3fc")
        self.log.tag_config("ok", foreground="#86efac")
        self.log.tag_config("fail", foreground="#fca5a5")
        self.log.tag_config("skip", foreground="#b5f97a")
        self.log.tag_config("dim", foreground="#6b7280")

    def _build_settings_tab(self):
        tab = self.tab_settings
        tab.grid_columnconfigure(1, weight=1)

        row = 0

        def section(text, r):
            ctk.CTkLabel(tab, text=text,
                         font=ctk.CTkFont(size=13, weight="bold"),
                         text_color="gray70").grid(
                row=r, column=0, columnspan=2, padx=4, pady=(18, 6), sticky="w")

        # ── Download ──────────────────────────────────────────────────────────
        section("Download", row)
        row += 1

        ctk.CTkLabel(tab, text="Default save folder:", anchor="w").grid(
            row=row, column=0, padx=(4, 8), pady=4, sticky="w")
        dest_row = ctk.CTkFrame(tab, fg_color="transparent")
        dest_row.grid(row=row, column=1, padx=(0, 4), pady=4, sticky="ew")
        dest_row.grid_columnconfigure(0, weight=1)
        ctk.CTkEntry(dest_row, textvariable=self.dest_var,
                     placeholder_text=f"Default: ./{DOWNLOAD_FOLDER}/  —  e.g. C:\\MyDownloads").grid(
            row=0, column=0, padx=(0, 6), sticky="ew")
        ctk.CTkButton(dest_row, text="Browse", width=80,
                      command=self._browse_dest_dir).grid(row=0, column=1)
        row += 1

        ctk.CTkLabel(tab, text="Max concurrent downloads:", anchor="w").grid(
            row=row, column=0, padx=(4, 8), pady=4, sticky="w")
        workers_frame = ctk.CTkFrame(tab, fg_color="transparent")
        workers_frame.grid(row=row, column=1, padx=(0, 4), pady=4, sticky="ew")
        self.workers_label = ctk.CTkLabel(workers_frame,
                                          text=str(MAX_WORKERS), width=24)
        self.workers_label.pack(side="right")
        self.workers_slider = ctk.CTkSlider(workers_frame, from_=1, to=5,
                                            number_of_steps=4,
                                            command=self._on_workers_change)
        self.workers_slider.set(MAX_WORKERS)
        self.workers_slider.pack(side="left", fill="x", expand=True, padx=(0, 8))
        row += 1

        # ── File filters ──────────────────────────────────────────────────────
        section("File filters", row)
        row += 1

        ctk.CTkLabel(tab, text="Include only (keywords):", anchor="w").grid(
            row=row, column=0, padx=(4, 8), pady=4, sticky="w")
        self.settings_include = ctk.CTkEntry(
            tab, placeholder_text="e.g.  mp4  jpg  png  — leave blank for everything")
        self.settings_include.grid(row=row, column=1, padx=(0, 4), pady=4, sticky="ew")
        row += 1

        ctk.CTkLabel(tab, text="Exclude (keywords):", anchor="w").grid(
            row=row, column=0, padx=(4, 8), pady=4, sticky="w")
        self.settings_exclude = ctk.CTkEntry(
            tab, placeholder_text="e.g.  .thumb  preview  sample")
        self.settings_exclude.grid(row=row, column=1, padx=(0, 4), pady=4, sticky="ew")
        row += 1

        ctk.CTkLabel(tab,
                     text="Space-separated keywords. Leave both blank to download everything.",
                     text_color="gray50",
                     font=ctk.CTkFont(size=11)).grid(
            row=row, column=0, columnspan=2, padx=4, pady=(2, 0), sticky="w")
        row += 1

        # ── Advanced ──────────────────────────────────────────────────────────
        section("Advanced", row)
        row += 1

        ctk.CTkLabel(tab, text="Max retries per file:", anchor="w").grid(
            row=row, column=0, padx=(4, 8), pady=4, sticky="w")
        retries_frame = ctk.CTkFrame(tab, fg_color="transparent")
        retries_frame.grid(row=row, column=1, padx=(0, 4), pady=4, sticky="ew")
        self.retries_label = ctk.CTkLabel(retries_frame,
                                          text=str(MAX_RETRIES), width=24)
        self.retries_label.pack(side="right")
        self.retries_slider = ctk.CTkSlider(retries_frame, from_=1, to=10,
                                            number_of_steps=9,
                                            command=self._on_retries_change)
        self.retries_slider.set(MAX_RETRIES)
        self.retries_slider.pack(side="left", fill="x", expand=True, padx=(0, 8))
        row += 1

        self.opt_no_disk_check = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(tab, text="Skip disk space check before downloading",
                        variable=self.opt_no_disk_check).grid(
            row=row, column=0, columnspan=2, padx=4, pady=4, sticky="w")
        row += 1

        self.opt_no_dl_folder = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(tab,
                        text='Skip the "Downloads" subfolder when using the default path '
                             "(a custom save folder already skips it automatically)",
                        variable=self.opt_no_dl_folder).grid(
            row=row, column=0, columnspan=2, padx=4, pady=4, sticky="w")
        row += 1

        self.opt_external_console = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            tab,
            text="Show detailed CLI window during downloads "
                 "(opens a separate console with full progress bars)",
            variable=self.opt_external_console).grid(
            row=row, column=0, columnspan=2, padx=4, pady=4, sticky="w")

    def _build_about_tab(self):
        tab = self.tab_about
        tab.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(tab, text="Bunkr Downloader",
                     font=ctk.CTkFont(size=26, weight="bold")).grid(
            row=0, column=0, pady=(30, 4))
        ctk.CTkLabel(tab, text=EDITION_TEXT,
                     font=ctk.CTkFont(size=13), text_color="gray60").grid(
            row=1, column=0, pady=(0, 4))
        ctk.CTkLabel(tab, text=f"Backend v{BACKEND_VERSION}",
                     font=ctk.CTkFont(size=11), text_color="gray50").grid(
            row=2, column=0, pady=(0, 20))

        ctk.CTkLabel(tab,
                     text=EDITION_DESCRIPTION,
                     wraplength=480, justify="center").grid(
            row=3, column=0, pady=(0, 28))

        ctk.CTkLabel(
            tab, text=ABOUT_TROUBLESHOOTING, wraplength=480,
            justify="center", text_color="#ffea00",
            font=ctk.CTkFont(size=12, weight="bold"),
        ).grid(row=4, column=0, pady=(0, 16))

        ctk.CTkButton(tab, text="GitHub Project",
                      fg_color="gray25", hover_color="gray35",
                      command=lambda: webbrowser.open(GITHUB_URL)).grid(
            row=5, column=0, pady=6)

        ctk.CTkLabel(tab, text="MIT License", text_color="gray50").grid(
            row=6, column=0, pady=(24, 0))

    def _build_home_heading(self, tab):
        """Show edition branding beside the existing main-page title."""
        heading = ctk.CTkFrame(tab, fg_color="transparent")
        heading.grid(row=0, column=0, padx=2, pady=(2, 6), sticky="ew")
        heading.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(heading, text="Bunkr Downloader",
                     font=ctk.CTkFont(size=20, weight="bold")).pack(side="left")
        ctk.CTkLabel(
            heading, text=HOME_BRANDING, corner_radius=6,
            fg_color="#30243a", text_color="#d8b4fe",
            font=ctk.CTkFont(size=10, weight="bold"),
        ).pack(side="right", padx=(8, 0), pady=2)

    def _build_shutdown_controls(self, tab):
        """Add the opt-in automatic shutdown checkbox and countdown controls."""
        card = ctk.CTkFrame(tab, fg_color=("gray90", "#202020"))
        card.grid(row=5, column=0, padx=0, pady=(0, 8), sticky="ew")
        card.grid_columnconfigure(0, weight=1)
        self.auto_shutdown_var = ctk.BooleanVar(value=DEFAULT_SHUTDOWN_ENABLED)
        ctk.CTkCheckBox(
            card,
            text="PC afsluiten wanneer alle downloads voltooid zijn",
            variable=self.auto_shutdown_var,
            command=self._on_shutdown_option_changed,
        ).grid(row=0, column=0, padx=10, pady=8, sticky="w")
        self.shutdown_option_status = ctk.CTkLabel(
            card,
            text="Actief — afsluiten na afronding van de volledige wachtrij.",
            text_color="#86efac", font=ctk.CTkFont(size=11), anchor="w",
        )
        self.shutdown_option_status.grid(
            row=1, column=0, padx=10, pady=(0, 6), sticky="w",
        )
        self.shutdown_option_status.grid_remove()
        self.shutdown_notice = ctk.CTkFrame(card, fg_color="transparent")
        self.shutdown_notice.grid_columnconfigure(0, weight=1)
        self.shutdown_notice_label = ctk.CTkLabel(
            self.shutdown_notice, text="", text_color="#fbbf24", anchor="w",
        )
        self.shutdown_notice_label.grid(row=0, column=0, padx=(10, 8), sticky="w")
        ctk.CTkButton(
            self.shutdown_notice, text="Cancel shutdown", width=130,
            fg_color="#7f1d1d", hover_color="#991b1b",
            command=self._cancel_shutdown,
        ).grid(row=0, column=1, padx=(0, 8), pady=(0, 6))
        self.shutdown_notice.grid_remove()

    def _on_shutdown_option_changed(self):
        if not self.auto_shutdown_var.get():
            self.shutdown_option_status.grid_remove()
            if getattr(self, "shutdown_manager", None) and self.shutdown_manager.pending:
                self._cancel_shutdown()
            return
        if self.auto_shutdown_var.get() and not messagebox.askyesno(
            "Confirm automatic shutdown",
            "The PC will shut down after the queue finishes. If any downloads fail, "
            "you will be asked again. A 30-second countdown can be cancelled. "
            "Save any other work before enabling this option.",
            parent=self,
        ):
            self.auto_shutdown_var.set(False)
            self.shutdown_option_status.grid_remove()
            return
        self.shutdown_option_status.grid(
            row=1, column=0, padx=10, pady=(0, 6), sticky="w",
        )

    def _update_shutdown_notice(self, remaining):
        if remaining is None or remaining <= 0:
            self.shutdown_notice.grid_remove()
            if remaining == 0:
                self.status_var.set("Windows shutdown is starting")
            return
        self.shutdown_notice_label.configure(
            text=f"All downloads completed. PC will shut down in {remaining} seconds.",
        )
        self.shutdown_notice.grid(row=2, column=0, columnspan=2, sticky="ew")

    def _cancel_shutdown(self):
        if self.shutdown_manager.cancel():
            self.auto_shutdown_var.set(False)
            self.shutdown_option_status.grid_remove()
            self.status_var.set("Shutdown cancelled")
        else:
            self.status_var.set("No active shutdown countdown to cancel")

    def _consider_auto_shutdown(self):
        """Only schedule shutdown once the batch worker and entire queue are idle."""
        if not self.auto_shutdown_var.get():
            return
        if self._batch_active or (
            self._worker_thread is not None and self._worker_thread.is_alive()
        ):
            self.after(100, self._consider_auto_shutdown)
            return

        statuses = [item["status"] for item in self._queue_items]
        state = queue_completion_state(
            statuses,
            batch_active=self._batch_active,
            stop_requested=self._stop_requested,
            active_process=self._active_proc is not None,
        )
        if state == "pending":
            self.status_var.set("Shutdown not scheduled — downloads remain in the queue")
            return
        if state == "empty":
            return
        if state == "failed":
            failed_count = sum(
                status in ("Failed", "Cancelled") for status in statuses
            )
            if not messagebox.askyesno(
                "Downloads did not all succeed",
                f"{failed_count} queued item(s) failed or were cancelled. "
                "Shut down anyway? A 30-second countdown can be cancelled.",
                parent=self,
            ):
                self.status_var.set("Shutdown not scheduled — review failed downloads")
                return
        elif state == "unverified":
            if not messagebox.askyesno(
                "Download results need review",
                "The external console did not report file-by-file results. "
                "Review it before shutting down. Shut down anyway? "
                "A 30-second countdown can be cancelled.",
                parent=self,
            ):
                self.status_var.set("Shutdown not scheduled — review download results")
                return

        if not self.shutdown_manager.schedule():
            self.status_var.set("Could not schedule Windows shutdown")

    # ─────────────────────────────────────────────────────────────────────────
    # UI helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _toggle_mode(self):
        if self.mode.get() == "url":
            self.url_frame.grid(row=2, column=0, padx=0, pady=(0, 9), sticky="ew")
            self.file_frame.grid_forget()
        else:
            self.file_frame.grid(row=2, column=0, padx=0, pady=(0, 9), sticky="ew")
            self.url_frame.grid_forget()

    def _paste_url(self):
        try:
            self.url_entry.delete(0, "end")
            self.url_entry.insert(0, self.clipboard_get())
        except Exception:
            self._log("Could not read from clipboard.\n")

    def _add_current_input(self):
        """Add one URL or all URLs from the selected text file to the queue."""
        if self.mode.get() == "url":
            url = self.url_entry.get().strip()
            if not url:
                self._log("Enter a Bunkr URL before adding it to the queue.\n", "fail")
                return
            self._add_queue_url(url)
            self.url_entry.delete(0, "end")
            self.url_entry.focus_set()
            return

        file_path = self.file_entry.get().strip()
        if not file_path:
            self._log("Select a .txt file containing URLs first.\n", "fail")
            return
        try:
            with open(file_path, "r", encoding="utf-8") as file:
                urls = [line.strip() for line in file if line.strip()]
        except (OSError, UnicodeError) as err:
            self._log(f"Could not read URL list: {err}\n", "fail")
            return
        if not urls:
            self._log("The selected file contains no URLs.\n", "fail")
            return
        for url in urls:
            self._add_queue_url(url)
        self.status_var.set(f"Added {len(urls)} URL(s) to the queue")

    def _add_queue_url(self, url):
        if getattr(self, "shutdown_manager", None) and self.shutdown_manager.pending:
            if self.shutdown_manager.cancel():
                self._log("Shutdown countdown cancelled because new work was queued.\n", "dim")
        self._queue_sequence += 1
        self._queue_items.append({
            "id": self._queue_sequence,
            "url": url,
            "status": "Waiting",
            "progress": 0.0,
        })
        self._render_queue()

    def _render_queue(self):
        """Render visible queue rows using the selected status filter."""
        for child in self.queue_list.winfo_children():
            child.destroy()
        filter_value = self._queue_filter.get()
        visible = []
        for item in self._queue_items:
            status = item["status"]
            if filter_value == "Active" and status not in ("Downloading", "Retrying"):
                continue
            if filter_value in ("Completed", "Skipped", "Failed", "Unverified") and status != filter_value:
                continue
            visible.append(item)

        if not visible:
            message = "No downloads yet — add a Bunkr URL above." if not self._queue_items else "No items match this filter."
            ctk.CTkLabel(self.queue_list, text=message, text_color="gray60").grid(
                row=0, column=0, padx=14, pady=18, sticky="w")

        for row, item in enumerate(visible):
            card = ctk.CTkFrame(self.queue_list, fg_color=("gray90", "#242424"),
                                corner_radius=7)
            card.grid(row=row, column=0, padx=3, pady=3, sticky="ew")
            card.grid_columnconfigure(0, weight=1)
            title = item["url"]
            if len(title) > 110:
                title = title[:107] + "…"
            ctk.CTkLabel(card, text=title, anchor="w", justify="left",
                         wraplength=650).grid(
                row=0, column=0, padx=10, pady=(7, 2), sticky="ew")
            status_color = {
                "Downloading": "#60a5fa",
                "Retrying": "#fbbf24",
                "Completed": "#4ade80",
                "Skipped": "#a3e635",
                "Failed": "#f87171",
                "Cancelled": "#fbbf24",
                "Unverified": "#fbbf24",
            }.get(item["status"], "gray65")
            ctk.CTkLabel(card, text=item["status"], text_color=status_color,
                         width=115, anchor="e").grid(
                row=0, column=1, padx=(4, 10), pady=(7, 2), sticky="e")
            bar = ctk.CTkProgressBar(card, height=6)
            bar.grid(row=1, column=0, columnspan=2, padx=10, pady=(2, 8), sticky="ew")
            bar.set(max(0, min(item["progress"], 100)) / 100)

        total = len(self._queue_items)
        waiting = sum(item["status"] == "Waiting" for item in self._queue_items)
        self.queue_count_var.set(f"{total} links  ·  {waiting} waiting")

    def _refresh_total_progress(self):
        done = self._downloaded_count + self._skipped_count + self._failed_count
        total = max(self._known_file_total, done)
        remaining = max(0, total - done)
        self._finished_file_total = done
        downloaded, skipped, failed = (
            self._downloaded_count, self._skipped_count, self._failed_count,
        )
        self.after(0, lambda: self.total_progress_var.set(f"{done} of {total} files"))
        self.after(0, lambda: self.progress.set(done / total if total else 0))
        self.after(0, lambda: self.counts_var.set(
            f"Downloaded {downloaded}   ·   Skipped {skipped}   ·   Failed {failed}"
            f"   ·   Remaining {remaining}"
        ))

    def _set_queue_item(self, item_id, *, status=None, progress=None):
        for item in self._queue_items:
            if item["id"] == item_id:
                if status is not None:
                    item["status"] = status
                if progress is not None:
                    item["progress"] = progress
                break
        if hasattr(self, "queue_list"):
            self.after(0, self._render_queue)

    def _clear_completed(self):
        self._queue_items = [item for item in self._queue_items
                             if item["status"] != "Completed"]
        self._render_queue()

    def _retry_failed(self):
        for item in self._queue_items:
            if item["status"] == "Failed":
                item["status"] = "Waiting"
                item["progress"] = 0
        self._render_queue()
        self._start_download()

    def _browse_input_file(self):
        path = filedialog.askopenfilename(
            title="Select URL list file",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if path:
            self.file_entry.delete(0, "end")
            self.file_entry.insert(0, path)

    def _browse_dest_dir(self):
        d = filedialog.askdirectory(title="Select download folder")
        if d:
            self.dest_var.set(d)

    def _open_dest(self):
        d = self.dest_var.get().strip() or DOWNLOAD_FOLDER
        if platform.system() == "Windows" and os.path.isdir(d):
            os.startfile(d)

    def _refresh_album_status(self):
        """Update the bottom status bar — safe to call from any thread."""
        with self._active_lock:
            done = self._album_done
            total = self._album_total
            active = list(self._active_files)

        url_part = (f"URL {self._url_index}/{self._url_total}  ·  "
                    if self._url_total > 1 else "")
        if total > 0:
            files_part = "  ·  " + ", ".join(active) if active else ""
            text = f"{url_part}File {done}/{total}{files_part}"
        else:
            text = f"{url_part}Starting…" if url_part else "Starting…"
        self.after(0, lambda t=text: self.status_var.set(t))

    def _on_workers_change(self, value):
        self.workers_label.configure(text=str(int(value)))

    def _on_retries_change(self, value):
        self.retries_label.configure(text=str(int(value)))

    def _log(self, text, tag=None):
        """Append text to the log textbox — safe to call from any thread."""
        def _append():
            self.log.configure(state="normal")
            if tag:
                self.log.insert("end", text, tag)
            else:
                self.log.insert("end", text)
            self.log.see("end")
            self.log.configure(state="disabled")
        self.after(0, _append)

    def _set_running(self, running: bool):
        """Update button/progress states — safe to call from any thread."""
        def _update():
            if running:
                self.run_btn.configure(state="disabled")
                self.pause_btn.configure(state="normal")
                self.stop_btn.configure(state="normal")
            else:
                self.run_btn.configure(state="normal")
                self.pause_btn.configure(state="disabled")
                self.stop_btn.configure(state="disabled")
        self.after(0, _update)

    # ─────────────────────────────────────────────────────────────────────────
    # Download logic
    # ─────────────────────────────────────────────────────────────────────────

    def _start_download(self):
        pending = [item.copy() for item in self._queue_items if item["status"] == "Waiting"]
        if not pending:
            self._log("Add one or more URLs to the queue before starting.\n", "dim")
            return

        self._stop_requested = False
        self._force_stop = False
        self._active_proc = None
        self._batch_active = True
        self.active_file_var.set("Waiting for a download")
        self.active_status_var.set("Starting")
        self.active_progress.set(0)
        self._set_running(True)
        self._url_index = 0
        self._url_total = len(pending)
        self.status_var.set(f"Starting {len(pending)} queued URL(s)…")
        self._worker_thread = threading.Thread(
            target=self._run_batch, args=(pending,), daemon=True)
        self._use_external_console = self.opt_external_console.get()
        self._worker_thread.start()

    def _pause(self):
        """Let the current URL finish and leave the rest queued.

        Partial files are kept for retry; already-completed files in the album
        are skipped automatically on the next run.
        """
        self._stop_requested = True
        self._log("\n[Stop after current requested — remaining URLs stay queued.]\n",
                  tag="dim")
        self.status_var.set("Stopping after current URL…")

    def _stop(self):
        """Force-kill the current download immediately.

        Confirm and then kill the child process immediately. Partial files are
        left behind and can be re-attempted on the next run.
        """
        if self._active_proc is not None and not messagebox.askyesno(
            "Cancel active download?",
            "The current URL will be interrupted. Any partial file will be kept for a retry. Continue?",
            parent=self,
        ):
            return
        self._stop_requested = True
        self._force_stop = True
        self._log("\n[Force stop — killing current download.]\n", tag="fail")
        self.status_var.set("Stopping…")
        if self._active_proc is not None:
            try:
                self._active_proc.kill()
            except Exception:
                pass

    def _build_downloader_args(self, url, *, include_ui_flag):
        """Build the argv list passed to the downloader for a single URL."""
        args = [url]
        if include_ui_flag:
            args.append("--disable-ui")

        dest = self.dest_var.get().strip()
        if dest:
            args += ["--custom-path", dest]

        include = self.settings_include.get().strip().split()
        exclude = self.settings_exclude.get().strip().split()
        if include:
            args += ["--include"] + include
        if exclude:
            args += ["--ignore"] + exclude

        args += ["--max-retries", str(int(self.retries_slider.get()))]
        args += ["--max-workers", str(int(self.workers_slider.get()))]
        if self.opt_no_disk_check.get():
            args.append("--disable-disk-check")
        if dest or self.opt_no_dl_folder.get():
            args.append("--no-download-folder")

        return args

    def _stream_subprocess(self, proc):
        """Read child stdout line-by-line, parse GUI markers, write the rest to log."""
        for raw in iter(proc.stdout.readline, ""):
            line = raw.rstrip("\r\n")
            if not line:
                self._log("\n")
                continue

            if line.startswith("__GUI_TOTAL__:"):
                try:
                    total = int(line.split(":", 1)[1])
                except ValueError:
                    continue
                with self._active_lock:
                    self._album_total = total
                    self._album_done = 0
                    self._active_files = []
                    self._known_file_total += total
                self._refresh_total_progress()
                self._refresh_album_status()
                continue

            if line.startswith("__GUI_FILE_START__:"):
                try:
                    event = json.loads(line.split(":", 1)[1])
                except (ValueError, json.JSONDecodeError):
                    continue
                filename = event.get("filename", "Unknown file")
                self.after(0, lambda name=filename: self.active_file_var.set(name))
                self.after(0, lambda: self.active_status_var.set("Downloading"))
                self.after(0, lambda: self.active_progress.set(0))
                continue

            if line.startswith("__GUI_FILE_PROGRESS__:"):
                try:
                    event = json.loads(line.split(":", 1)[1])
                    percent = max(0.0, min(float(event.get("percent") or 0), 100.0))
                except (ValueError, TypeError, json.JSONDecodeError):
                    continue
                self.after(0, lambda value=percent: self.active_progress.set(value / 100))
                self.after(0, lambda value=percent: self.active_status_var.set(
                    f"Downloading · {value:.0f}%"))
                current_id = getattr(self, "_current_queue_id", None)
                if current_id is not None:
                    self._set_queue_item(current_id, status="Downloading")
                continue

            if line.startswith("__GUI_FILE_RESULT__:"):
                try:
                    event = json.loads(line.split(":", 1)[1])
                    result = event.get("result", "")
                except (ValueError, json.JSONDecodeError):
                    continue
                statuses = {
                    "DOWNLOAD_SUCCESS": "Completed",
                    "ALREADY_DOWNLOADED": "Skipped · already exists",
                    "IGNORE_LIST": "Skipped",
                    "INCLUDE_LIST": "Skipped",
                }
                self.after(0, lambda value=statuses.get(result, "Failed"):
                           self.active_status_var.set(value))
                continue

            if line.startswith("__GUI_STATE__:"):
                try:
                    event = json.loads(line.split(":", 1)[1])
                except (ValueError, json.JSONDecodeError):
                    continue
                attempt = re.search(r"\((\d+)/(\d+)\)", event.get("details", ""))
                label = f"Retrying {attempt.group(1)}/{attempt.group(2)}" if attempt else "Retrying"
                self.after(0, lambda value=label: self.active_status_var.set(value))
                current_id = getattr(self, "_current_queue_id", None)
                if current_id is not None:
                    self._set_queue_item(current_id, status="Retrying")
                self._log(f"Retrying: {event.get('details', '')}\n", tag="dim")
                continue

            if line.startswith("__GUI_COUNT__:"):
                category = line.split(":", 1)[1].strip()
                if category == "downloaded":
                    self._downloaded_count += 1
                elif category == "skipped":
                    self._skipped_count += 1
                elif category == "failed":
                    self._failed_count += 1
                self._refresh_total_progress()
                current_id = getattr(self, "_current_queue_id", None)
                album_total = self._album_total
                if current_id is not None and album_total:
                    finished = (
                        self._downloaded_count + self._skipped_count + self._failed_count
                    ) - getattr(self, "_queue_start_done", 0)
                    self._set_queue_item(
                        current_id, progress=min(100, finished * 100 / album_total),
                    )
                continue

            if line.startswith("__GUI_START__:"):
                fname = line.split(":", 1)[1]
                with self._active_lock:
                    self._active_files.append(fname)
                self._refresh_album_status()
                self._log(f"  → {fname}\n")
                continue

            if line.startswith("__GUI_END__:"):
                fname = line.split(":", 1)[1]
                with self._active_lock:
                    if fname in self._active_files:
                        self._active_files.remove(fname)
                    self._album_done += 1
                self._refresh_album_status()
                continue

            # Plain log line from the backend — colorize via the IORedirector
            # classifier logic (same keywords).
            lower = line.lower()
            if "download failed" in lower or "error" in lower or "exceeded" in lower:
                tag = "fail"
            elif "skipped" in lower or "already been downloaded" in lower:
                tag = "skip"
            elif "completed" in lower or "success" in lower:
                tag = "ok"
            else:
                tag = None
            self._log(line + "\n", tag=tag)

        proc.stdout.close()

    def _self_invoke_cmd(self, extra_args):
        """Build the command line that re-launches this program in runner mode.

        Works for both the PyInstaller bundle (self-execs the .exe) and source
        mode (re-invokes python.exe with gui.py)."""
        sentinel = "--gui-runner"
        if IS_FROZEN:
            return [sys.executable, sentinel, *extra_args]
        script = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else "gui.py"
        return [_child_python_exe(), script, sentinel, *extra_args]

    def _run_batch(self, queue_items):
        import subprocess
        external = self._use_external_console
        total = len(queue_items)
        completed = 0
        failed = 0

        for i, queued_item in enumerate(queue_items):
            url = queued_item["url"]
            queue_id = queued_item["id"]
            if self._stop_requested:
                self._log(f"\n[Stopped — skipping {total - i} remaining URL(s)]\n",
                          tag="dim")
                self.after(0, lambda: self.status_var.set(
                    f"Stopped — {completed} done, {failed} failed"))
                break

            self._set_queue_item(queue_id, status="Downloading", progress=0)
            self._current_queue_id = queue_id
            count_start = (
                self._downloaded_count, self._skipped_count, self._failed_count,
            )

            # Reset per-album counters; the LiveManager patch will fill in the
            # totals once the album page is parsed.
            with self._active_lock:
                self._album_total = 0
                self._album_done = 0
                self._active_files = []
                self._queue_start_done = sum(count_start)
            self._url_index = i + 1
            self._url_total = total
            self._refresh_album_status()

            self._log(f"\n{'─' * 60}\n", tag="header")
            self._log(f"  [{i + 1}/{total}]  {url}\n", tag="header")
            self._log(f"{'─' * 60}\n\n", tag="header")

            self._force_stop = False
            try:
                if external:
                    # Detached console with the full Rich UI (progress bars +
                    # log table). No stdout capture, no GUI markers.
                    cmd = [sys.executable, "downloader.py",
                           *self._build_downloader_args(url, include_ui_flag=False)]
                    self._log("  (running in detached console window)\n", tag="dim")
                    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
                    self._active_proc = subprocess.Popen(cmd, creationflags=flags)
                    rc = self._active_proc.wait()
                else:
                    # Hidden child — either the bundled .exe self-invoking in
                    # runner mode, or `python gui.py --gui-runner …` in source
                    # mode. Killable instantly via Stop.
                    cmd = self._self_invoke_cmd(
                        self._build_downloader_args(url, include_ui_flag=True))
                    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    self._active_proc = subprocess.Popen(
                        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8", errors="replace",
                        bufsize=1, creationflags=flags,
                    )
                    self._stream_subprocess(self._active_proc)
                    rc = self._active_proc.wait()

                self._active_proc = None
                if self._force_stop:
                    failed += 1
                    self._set_queue_item(queue_id, status="Cancelled")
                    self._log(f"\n  [STOPPED]  {url}\n", tag="fail")
                elif rc == 0:
                    completed += 1
                    new_downloads = self._downloaded_count - count_start[0]
                    new_skips = self._skipped_count - count_start[1]
                    new_failures = self._failed_count - count_start[2]
                    if new_failures:
                        row_status = "Failed"
                    elif new_skips and not new_downloads and not new_failures:
                        row_status = "Skipped"
                    elif external or (not new_downloads and not new_skips):
                        # The detached console has no result markers, and an empty
                        # result stream cannot prove that planned files succeeded.
                        row_status = "Unverified"
                    else:
                        row_status = "Completed"
                    self._set_queue_item(queue_id, status=row_status, progress=100)
                    self._log(f"\n  [{row_status.upper()}]  Finished: {url}\n",
                              tag="skip" if row_status == "Skipped" else "ok")
                else:
                    failed += 1
                    self._set_queue_item(queue_id, status="Failed")
                    self._log(f"\n  [FAIL]  {url} exited with code {rc}\n",
                              tag="fail")

            except Exception as e:
                failed += 1
                self._set_queue_item(queue_id, status="Failed")
                self._log(f"\n  [FAIL]  Error downloading {url}:\n     {e}\n",
                          tag="fail")

            self._current_queue_id = None
            if self._album_total:
                self._set_queue_item(queue_id, progress=100)

        else:
            # Loop completed without break
            summary = f"Done — {completed} succeeded"
            if failed:
                summary += f", {failed} failed"
            self._log(f"\n{'═' * 60}\n  {summary}\n{'═' * 60}\n")
            self.after(0, lambda s=summary: self.status_var.set(s))

        if self._stop_requested:
            # The rest stay in Waiting state and can be started later.
            for queued_item in queue_items[i + 1:]:
                self._set_queue_item(queued_item["id"], status="Waiting")

        # Clear the per-file label so it doesn't show stale info
        with self._active_lock:
            self._album_total = 0
            self._album_done = 0
            self._active_files = []
        self._refresh_album_status()
        self.after(0, self._render_queue)
        if self._force_stop:
            self.after(0, lambda: self.status_var.set(
                "Cancelled — remaining URLs are still queued"))

        self._set_running(False)
        self._batch_active = False
        self.after(0, self._consider_auto_shutdown)


def _run_as_gui_runner():
    """Run the downloader inside this process, emitting GUI marker lines.

    Triggered when the executable is launched with `--gui-runner` as the first
    argument. The bundled .exe self-invokes in this mode so the GUI can spawn
    download children without needing a separate runner binary alongside.
    """
    import asyncio
    import logging

    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")

    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)

    from src.downloaders import media_downloader as _md
    from src.managers import live_manager as _lm

    file_context = threading.local()

    _orig_add = _lm.LiveManager.add_overall_task

    def _wrap_add(self, description, num_tasks):
        print(f"__GUI_TOTAL__:{num_tasks}", flush=True)
        return _orig_add(self, description, num_tasks)

    _lm.LiveManager.add_overall_task = _wrap_add

    _orig_update_task = _lm.LiveManager.update_task

    def _wrap_update_task(self, task_id, completed=None, advance=0, *, visible=True):
        filename = getattr(file_context, "filename", None)
        if filename is not None and completed is not None:
            print("__GUI_FILE_PROGRESS__:" + json.dumps({
                "filename": filename,
                "percent": completed,
            }), flush=True)
        return _orig_update_task(
            self, task_id, completed, advance, visible=visible,
        )

    _lm.LiveManager.update_task = _wrap_update_task

    _orig_update_summary = _lm.LiveManager.update_summary

    def _wrap_update_summary(self, reason):
        filename = getattr(file_context, "filename", None)
        reason_name = getattr(reason, "name", "")
        if reason_name == "DOWNLOAD_SUCCESS":
            category = "downloaded"
        elif reason_name in {
            "ALREADY_DOWNLOADED", "IGNORE_LIST", "INCLUDE_LIST",
            "DOMAIN_OFFLINE", "SERVICE_UNAVAILABLE",
        }:
            category = "skipped"
        else:
            category = "failed"
        print(f"__GUI_COUNT__:{category}", flush=True)
        if filename is not None:
            print("__GUI_FILE_RESULT__:" + json.dumps({
                "filename": filename,
                "result": reason_name,
            }), flush=True)
        return _orig_update_summary(self, reason)

    _lm.LiveManager.update_summary = _wrap_update_summary

    _orig_update_log = _lm.LiveManager.update_log

    def _wrap_update_log(self, *, event, details):
        if event.lower().startswith("retry"):
            print("__GUI_STATE__:" + json.dumps({"details": details}), flush=True)
        if event == "Skipped download":
            filename = details
            for marker in (" has already been downloaded", " matches the ignore list",
                           " is not included"):
                filename = filename.split(marker, 1)[0]
            print("__GUI_FILE_START__:" + json.dumps({"filename": filename}), flush=True)
            print("__GUI_FILE_RESULT__:" + json.dumps({
                "filename": filename,
                "result": "ALREADY_DOWNLOADED",
            }), flush=True)
            if getattr(file_context, "filename", None) is None:
                print(f"__GUI_START__:{_safe_text(filename)}", flush=True)
                print(f"__GUI_END__:{_safe_text(filename)}", flush=True)
        return _orig_update_log(self, event=event, details=details)

    _lm.LiveManager.update_log = _wrap_update_log

    _orig_dl = _md.MediaDownloader.download

    def _safe_text(s: str) -> str:
        enc = sys.stdout.encoding or "utf-8"
        return s.encode(enc, errors="replace").decode(enc)

    def _wrap_dl(self):
        fname = _safe_text(self.download_info.filename)
        file_context.filename = fname
        print("__GUI_FILE_START__:" + json.dumps({"filename": fname}), flush=True)
        print(f"__GUI_START__:{fname}", flush=True)
        try:
            return _orig_dl(self)
        finally:
            print(f"__GUI_END__:{fname}", flush=True)
            file_context.filename = None

    _md.MediaDownloader.download = _wrap_dl

    import downloader as _d
    _d.clear_terminal = lambda: None
    asyncio.run(_d.main())


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--gui-runner":
        # Strip the sentinel so the downstream argparse sees the real args.
        del sys.argv[1]
        _run_as_gui_runner()
        sys.exit(0)

    app = DownloaderUI()
    app.mainloop()
