"""
Shared application resources — currently just the window/taskbar icon.

To use your own icon: drop a file at assets/icon.ico (Windows/Linux) and
assets/icon.png (used as a fallback, and by platforms that don't read
.ico) at the project root, replacing the defaults generated for this
project. No code changes needed — get_app_icon() picks them up
automatically. If neither file is present, the app falls back to the
platform's default window icon instead of crashing.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtGui import QIcon

logger = logging.getLogger("ui.resources")


_ROOT = Path(__file__).resolve().parent.parent
_ICON_CANDIDATES = (
    _ROOT / "assets" / "icon.ico",
    _ROOT / "assets" / "icon.png",
)

_cached_icon: QIcon | None = None


def get_app_icon() -> QIcon:
    """
    The application icon, loaded once and cached.

    Tries assets/icon.ico first, then assets/icon.png. Returns an empty
    QIcon (Qt/OS default) if neither exists or fails to load — callers
    can pass this straight to setWindowIcon()/setApplicationIcon()
    without any extra checks.
    """
    global _cached_icon
    if _cached_icon is not None:
        return _cached_icon

    for path in _ICON_CANDIDATES:
        if not path.exists():
            continue
        icon = QIcon(str(path))
        if not icon.isNull():
            _cached_icon = icon
            return icon
        logger.warning(f"Found icon file but Qt could not load it: {path}")

    logger.info(
        "No app icon found at assets/icon.ico or assets/icon.png — "
        "using the platform default."
    )
    _cached_icon = QIcon()
    return _cached_icon
