"""
AI Story Studio — entry point.

Run with:
    python main.py

Requires PySide6 and (optionally, for actual inference) llama-cpp-python.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# Ensure the project root is on sys.path so `engine.*` and `ui.*` imports
# resolve correctly no matter what directory this is launched from.
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _configure_logging() -> None:
    """
    Console logging for the whole app. Every module logs through
    logging.getLogger(__name__) and inherits this root configuration
    (engine.chat's "llm_engine" logger is the one exception — it sets up
    its own handler with the same format and disables propagation, so it
    won't double-print).
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


_configure_logging()
logger = logging.getLogger("main")

from PySide6.QtWidgets import QApplication

from engine import storage
from ui.main import MainWindow
from ui.styles import MAIN_STYLESHEET


def main() -> int:
    logger.info("Starting AI Story Studio...")
    storage.ensure_data_dir()

    app = QApplication(sys.argv)
    app.setApplicationName("AI Story Studio")
    app.setStyleSheet(MAIN_STYLESHEET)

    window = MainWindow()
    window.show()

    logger.info("Main window ready.")
    exit_code = app.exec()
    logger.info(f"AI Story Studio exiting (code {exit_code}).")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
