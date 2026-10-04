"""Opt-in, cancelable Windows shutdown after a fully settled download queue."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Sequence


DEFAULT_SHUTDOWN_ENABLED = False
SHUTDOWN_COUNTDOWN_SECONDS = 30

_SUCCESS_STATUSES = {"Completed", "Skipped", "Skipped · already exists"}
_FAILURE_STATUSES = {"Failed", "Cancelled"}
_UNVERIFIED_STATUSES = {"Unverified"}


def queue_completion_state(
    statuses: Sequence[str], *, batch_active: bool,
    stop_requested: bool, active_process: bool,
) -> str:
    """Classify a queue for shutdown gating; unknown states fail closed as pending."""
    if not statuses:
        return "empty"
    if batch_active or stop_requested or active_process:
        return "pending"
    terminal_statuses = _SUCCESS_STATUSES | _FAILURE_STATUSES | _UNVERIFIED_STATUSES
    if any(status not in terminal_statuses for status in statuses):
        return "pending"
    if any(status in _FAILURE_STATUSES for status in statuses):
        return "failed"
    if any(status in _UNVERIFIED_STATUSES for status in statuses):
        return "unverified"
    return "ready"


class ShutdownManager:
    """Start and cancel a normal Windows shutdown countdown without forcing apps."""

    def __init__(
        self, root, on_countdown: Callable[[int | None], None], *,
        seconds: int = SHUTDOWN_COUNTDOWN_SECONDS,
        command_runner: Callable[..., object] | None = None,
        platform_name: str | None = None,
    ) -> None:
        self.root = root
        self.on_countdown = on_countdown
        self.seconds = seconds
        self.command_runner = command_runner or subprocess.run
        self.platform_name = platform_name or os.name
        self._remaining = 0
        self._timer_id = None
        self.pending = False

    def _run(self, args: list[str]) -> bool:
        if self.platform_name != "nt":
            return False
        try:
            self.command_runner(
                args,
                check=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return True

    def schedule(self) -> bool:
        """Schedule a non-forced shutdown after the configured countdown."""
        if self.pending or not self._run([
            "shutdown.exe", "/s", "/t", str(self.seconds),
            "/c", "All queued downloads have finished.",
        ]):
            return False
        self.pending = True
        self._remaining = self.seconds
        self.on_countdown(self._remaining)
        self._timer_id = self.root.after(1000, self._tick)
        return True

    def _tick(self) -> None:
        if not self.pending:
            return
        self._remaining -= 1
        if self._remaining <= 0:
            self.pending = False
            self._timer_id = None
            self.on_countdown(0)
            return
        self.on_countdown(self._remaining)
        self._timer_id = self.root.after(1000, self._tick)

    def cancel(self) -> bool:
        """Cancel a scheduled Windows shutdown and its GUI countdown."""
        if not self.pending or not self._run(["shutdown.exe", "/a"]):
            return False
        if self._timer_id is not None:
            try:
                self.root.after_cancel(self._timer_id)
            except Exception:
                pass
        self.pending = False
        self._remaining = 0
        self._timer_id = None
        self.on_countdown(None)
        return True
