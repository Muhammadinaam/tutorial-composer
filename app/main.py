from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from app.ui.main_window import MainWindow


def load_theme(app: QApplication) -> None:
    qss = Path(__file__).parent / "ui" / "theme.qss"
    if qss.is_file():
        app.setStyleSheet(qss.read_text(encoding="utf-8"))


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("Tutorial Composer")
    app.setOrganizationName("TutorialComposer")
    load_theme(app)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
