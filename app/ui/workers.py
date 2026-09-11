from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, QThread, Signal


class TaskWorker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)
    progress = Signal(str)
    percent = Signal(int)

    def __init__(self, fn: Callable, parent: QObject | None = None):
        super().__init__(parent)
        self._fn = fn

    def _report(self, message: str = "", percent: int | float | None = None) -> None:
        if message:
            self.progress.emit(str(message))
        if percent is not None:
            self.percent.emit(max(0, min(100, int(percent))))

    def run(self) -> None:
        try:
            result = self._fn(self._report)
            self.succeeded.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))
