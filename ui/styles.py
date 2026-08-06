"""
UI theme constants and stylesheets for AI Story Studio.
Dark ink-on-paper aesthetic — like a serious writing tool, not a chatbot.
"""

# Palette
COLOR_BG = "#0f0f11"
COLOR_SURFACE = "#17171a"
COLOR_SURFACE_RAISED = "#1f1f24"
COLOR_BORDER = "#2a2a30"
COLOR_BORDER_LIGHT = "#38383f"
COLOR_ACCENT = "#7c5cbf"          # deep violet
COLOR_ACCENT_HOVER = "#9370db"
COLOR_ACCENT_DIM = "#3d2d60"
COLOR_TEXT = "#e8e6f0"
COLOR_TEXT_DIM = "#8b8899"
COLOR_TEXT_MUTED = "#5a5868"
COLOR_SUCCESS = "#3d9970"
COLOR_WARNING = "#e8a045"
COLOR_ERROR = "#c0392b"
COLOR_USER_MSG = "#1a2035"
COLOR_ASSISTANT_MSG = "#181820"
COLOR_SUMMARY_MSG = "#1a1a15"

FONT_MONO = "JetBrains Mono, Fira Code, Consolas, monospace"
FONT_SANS = "Inter, Segoe UI, SF Pro Text, system-ui, sans-serif"
FONT_SERIF = "Georgia, 'Times New Roman', serif"


MAIN_STYLESHEET = f"""
/* ── Base ── */
QWidget {{
    background-color: {COLOR_BG};
    color: {COLOR_TEXT};
    font-family: {FONT_SANS};
    font-size: 14px;
    border: none;
    outline: none;
}}

QMainWindow {{
    background-color: {COLOR_BG};
}}

/* ── Scroll bars ── */
QScrollBar:vertical {{
    background: {COLOR_SURFACE};
    width: 8px;
    margin: 0;
    border-radius: 4px;
}}
QScrollBar::handle:vertical {{
    background: {COLOR_BORDER_LIGHT};
    border-radius: 4px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{
    background: {COLOR_ACCENT};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{
    background: {COLOR_SURFACE};
    height: 8px;
    border-radius: 4px;
}}
QScrollBar::handle:horizontal {{
    background: {COLOR_BORDER_LIGHT};
    border-radius: 4px;
    min-width: 30px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

/* ── Splitter ── */
QSplitter::handle {{
    background: {COLOR_BORDER};
}}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}

/* ── Tab Bar ── */
QTabBar::tab {{
    background: {COLOR_SURFACE};
    color: {COLOR_TEXT_DIM};
    padding: 10px 22px;
    border: none;
    border-bottom: 2px solid transparent;
    font-size: 14px;
}}
QTabBar::tab:selected {{
    color: {COLOR_TEXT};
    border-bottom: 2px solid {COLOR_ACCENT};
}}
QTabBar::tab:hover:!selected {{
    color: {COLOR_TEXT};
    background: {COLOR_SURFACE_RAISED};
}}
QTabWidget::pane {{
    border: 1px solid {COLOR_BORDER};
    border-top: none;
}}

/* ── Buttons ── */
QPushButton {{
    background-color: {COLOR_SURFACE_RAISED};
    color: {COLOR_TEXT};
    border: 1px solid {COLOR_BORDER};
    border-radius: 6px;
    padding: 8px 16px;
    font-size: 14px;
}}
QPushButton:hover {{
    background-color: {COLOR_BORDER};
    border-color: {COLOR_BORDER_LIGHT};
}}
QPushButton:pressed {{
    background-color: {COLOR_ACCENT_DIM};
}}
QPushButton:disabled {{
    color: {COLOR_TEXT_MUTED};
    border-color: {COLOR_BORDER};
}}

QPushButton#accent {{
    background-color: {COLOR_ACCENT};
    color: white;
    border: none;
    font-weight: 600;
}}
QPushButton#accent:hover {{
    background-color: {COLOR_ACCENT_HOVER};
}}
QPushButton#accent:disabled {{
    background-color: {COLOR_ACCENT_DIM};
    color: {COLOR_TEXT_MUTED};
}}

QPushButton#danger {{
    background-color: transparent;
    color: {COLOR_ERROR};
    border: 1px solid {COLOR_ERROR};
}}
QPushButton#danger:hover {{
    background-color: {COLOR_ERROR};
    color: white;
}}

QPushButton#subtle {{
    background: transparent;
    border: none;
    color: {COLOR_TEXT_DIM};
    padding: 4px 8px;
}}
QPushButton#subtle:hover {{
    color: {COLOR_TEXT};
    background: {COLOR_SURFACE_RAISED};
    border-radius: 4px;
}}

/* ── Text inputs ── */
QLineEdit, QTextEdit, QPlainTextEdit {{
    background-color: {COLOR_SURFACE};
    color: {COLOR_TEXT};
    border: 1px solid {COLOR_BORDER};
    border-radius: 6px;
    padding: 8px 12px;
    font-size: 14px;
    selection-background-color: {COLOR_ACCENT_DIM};
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {{
    border-color: {COLOR_ACCENT};
}}

/* ── ComboBox ── */
QComboBox {{
    background-color: {COLOR_SURFACE};
    color: {COLOR_TEXT};
    border: 1px solid {COLOR_BORDER};
    border-radius: 6px;
    padding: 5px 10px;
    min-width: 120px;
}}
QComboBox:hover {{
    border-color: {COLOR_BORDER_LIGHT};
}}
QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 20px;
    border: none;
}}
QComboBox::down-arrow {{
    width: 10px;
    height: 10px;
}}
QComboBox QAbstractItemView {{
    background-color: {COLOR_SURFACE_RAISED};
    color: {COLOR_TEXT};
    border: 1px solid {COLOR_BORDER};
    selection-background-color: {COLOR_ACCENT_DIM};
    outline: none;
}}

/* ── List Widget ── */
QListWidget {{
    background-color: {COLOR_SURFACE};
    border: 1px solid {COLOR_BORDER};
    border-radius: 6px;
    padding: 6px;
    outline: none;
}}
QListWidget::item {{
    padding: 10px 12px;
    border-radius: 6px;
    color: {COLOR_TEXT};
    min-height: 20px;
}}
QListWidget::item:selected {{
    background-color: {COLOR_ACCENT_DIM};
    color: {COLOR_TEXT};
}}
QListWidget::item:hover:!selected {{
    background-color: {COLOR_SURFACE_RAISED};
}}

/* ── Tree Widget ── */
QTreeWidget {{
    background-color: {COLOR_SURFACE};
    border: 1px solid {COLOR_BORDER};
    border-radius: 6px;
    outline: none;
}}
QTreeWidget::item {{
    padding: 4px 6px;
}}
QTreeWidget::item:selected {{
    background-color: {COLOR_ACCENT_DIM};
    color: {COLOR_TEXT};
}}
QTreeWidget::item:hover:!selected {{
    background-color: {COLOR_SURFACE_RAISED};
}}
QHeaderView::section {{
    background-color: {COLOR_SURFACE_RAISED};
    color: {COLOR_TEXT_DIM};
    padding: 4px 8px;
    border: none;
    border-bottom: 1px solid {COLOR_BORDER};
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
}}

/* ── Labels ── */
QLabel {{
    color: {COLOR_TEXT};
    background: transparent;
}}
QLabel#heading {{
    font-size: 20px;
    font-weight: 700;
    color: {COLOR_TEXT};
}}
QLabel#subheading {{
    font-size: 14px;
    color: {COLOR_TEXT_DIM};
}}
QLabel#muted {{
    color: {COLOR_TEXT_MUTED};
    font-size: 13px;
}}
QLabel#status {{
    color: {COLOR_ACCENT};
    font-size: 13px;
}}

/* ── Group Box ── */
QGroupBox {{
    border: 1px solid {COLOR_BORDER};
    border-radius: 8px;
    margin-top: 16px;
    padding-top: 12px;
    color: {COLOR_TEXT_DIM};
    font-size: 12px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: {COLOR_TEXT_DIM};
    text-transform: uppercase;
    letter-spacing: 0.08em;
    font-size: 11px;
}}

/* ── Spin Box ── */
QSpinBox {{
    background-color: {COLOR_SURFACE};
    color: {COLOR_TEXT};
    border: 1px solid {COLOR_BORDER};
    border-radius: 6px;
    padding: 5px 8px;
}}
QSpinBox:focus {{
    border-color: {COLOR_ACCENT};
}}

/* ── Check Box ── */
QCheckBox {{
    color: {COLOR_TEXT};
    spacing: 8px;
}}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {COLOR_BORDER_LIGHT};
    border-radius: 4px;
    background: {COLOR_SURFACE};
}}
QCheckBox::indicator:checked {{
    background: {COLOR_ACCENT};
    border-color: {COLOR_ACCENT};
}}

/* ── Progress Bar ── */
QProgressBar {{
    background-color: {COLOR_SURFACE};
    border: 1px solid {COLOR_BORDER};
    border-radius: 4px;
    height: 6px;
    text-align: center;
    color: transparent;
}}
QProgressBar::chunk {{
    background-color: {COLOR_ACCENT};
    border-radius: 4px;
}}

/* ── Tooltip ── */
QToolTip {{
    background-color: {COLOR_SURFACE_RAISED};
    color: {COLOR_TEXT};
    border: 1px solid {COLOR_BORDER};
    padding: 4px 8px;
    border-radius: 4px;
    font-size: 12px;
}}

/* ── Menu ── */
QMenu {{
    background-color: {COLOR_SURFACE_RAISED};
    border: 1px solid {COLOR_BORDER};
    border-radius: 6px;
    padding: 4px;
}}
QMenu::item {{
    padding: 6px 16px;
    border-radius: 4px;
    color: {COLOR_TEXT};
}}
QMenu::item:selected {{
    background-color: {COLOR_ACCENT_DIM};
}}
QMenu::separator {{
    height: 1px;
    background: {COLOR_BORDER};
    margin: 4px 8px;
}}

/* ── Status Bar ── */
QStatusBar {{
    background-color: {COLOR_SURFACE};
    border-top: 1px solid {COLOR_BORDER};
    color: {COLOR_TEXT_DIM};
    font-size: 12px;
}}
"""
