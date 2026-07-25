"""
AI Story Studio — entry point.

Run with:
    python main.py

Requires PySide6 and (optionally, for actual inference) llama-cpp-python.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the project root is on sys.path so `engine.*` and `ui.*` imports
# resolve correctly no matter what directory this is launched from.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtWidgets import QApplication

from engine import storage
from ui.main import MainWindow
from ui.styles import MAIN_STYLESHEET


def main() -> int:
    storage.ensure_data_dir()

    app = QApplication(sys.argv)
    app.setApplicationName("AI Story Studio")
    app.setStyleSheet(MAIN_STYLESHEET)

    window = MainWindow()
    window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
