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
from ui.resources import get_app_icon
from ui.styles import MAIN_STYLESHEET


def _set_windows_taskbar_identity() -> None:
    """
    Windows groups taskbar icons by an "AppUserModelID" rather than by
    the running executable. Without setting our own, a python.exe-launched
    app inherits Python's generic icon in the taskbar/alt-tab even after
    setWindowIcon() — the title-bar icon updates but the taskbar one
    doesn't. This is a no-op (and safe to skip) on any other OS.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "AnthropicCommunity.AIStoryStudio.App.1"
        )
    except Exception:
        logger.debug("Could not set Windows AppUserModelID (non-fatal).", exc_info=True)


def main() -> int:
    logger.info("Starting AI Story Studio...")
    storage.ensure_data_dir()
    _set_windows_taskbar_identity()

    app = QApplication(sys.argv)
    app.setApplicationName("AI Story Studio")
    app.setStyleSheet(MAIN_STYLESHEET)
    app.setWindowIcon(get_app_icon())

    window = MainWindow()
    window.show()

    logger.info("Main window ready.")
    exit_code = app.exec()
    logger.info(f"AI Story Studio exiting (code {exit_code}).")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
