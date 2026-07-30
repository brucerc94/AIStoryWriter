"""
Story Panel.

Displays and edits all story content:
- Synopsis
- Outline
- Characters
- World
- Chapters (list + editor)

Also provides action buttons that trigger workflow tasks via the Chat panel.
"""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from engine import storage
from engine.models import Chapter, Character, Project, TaskType
from ui.styles import (
    COLOR_ACCENT,
    COLOR_BORDER,
    COLOR_ERROR,
    COLOR_SURFACE,
    COLOR_SURFACE_RAISED,
    COLOR_TEXT,
    COLOR_TEXT_DIM,
    COLOR_TEXT_MUTED,
    COLOR_WARNING,
    FONT_SERIF,
)
from ui.widgets import SizeAdjustingTabWidget


class SectionHeader(QWidget):
    def __init__(self, title: str, action_label: str = "", parent=None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        lbl = QLabel(title)
        lbl.setStyleSheet(
            f"font-size: 15px; font-weight: 700; color: {COLOR_TEXT};"
        )
        layout.addWidget(lbl)
        layout.addStretch()
        self.action_btn: Optional[QPushButton] = None
        if action_label:
            self.action_btn = QPushButton(action_label)
            self.action_btn.setObjectName("accent")
            layout.addWidget(self.action_btn)


class MarkdownEditor(QWidget):
    """A labeled text editor for markdown content with a save/generate button row."""

    content_saved = Signal(str)

    def __init__(
        self,
        placeholder: str = "",
        font_serif: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.editor = QTextEdit()
        self.editor.setPlaceholderText(placeholder)
        style = f"""
            QTextEdit {{
                background: {COLOR_SURFACE};
                border: 1px solid {COLOR_BORDER};
                border-radius: 8px;
                padding: 12px;
                font-size: {'15px' if font_serif else '13px'};
                color: {COLOR_TEXT};
                {'font-family: ' + FONT_SERIF + ';' if font_serif else ''}
                line-height: 1.6;
            }}
            QTextEdit:focus {{ border-color: {COLOR_ACCENT}; }}
        """
        self.editor.setStyleSheet(style)
        layout.addWidget(self.editor, 1)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 0, 0)
        self.save_btn = QPushButton("Save")
        self.save_btn.clicked.connect(self._save)
        btn_row.addStretch()
        btn_row.addWidget(self.save_btn)
        layout.addLayout(btn_row)

    def set_text(self, text: str) -> None:
        self.editor.setPlainText(text)

    def get_text(self) -> str:
        return self.editor.toPlainText()

    def _save(self) -> None:
        self.content_saved.emit(self.get_text())


class SynopsisTab(QWidget):
    task_requested = Signal(TaskType, str)
    content_changed = Signal(str)  # synopsis text

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        header = SectionHeader("Synopsis", "Generate with AI")
        if header.action_btn:
            header.action_btn.clicked.connect(
                lambda: self.task_requested.emit(TaskType.WRITE_SYNOPSIS, "")
            )
        layout.addWidget(header)

        self.editor = MarkdownEditor(
            placeholder=(
                "Write your story synopsis here, or use 'Generate with AI' to create one.\n\n"
                "A good synopsis covers: premise, main characters, central conflict, and stakes."
            )
        )
        self.editor.content_saved.connect(self.content_changed.emit)
        layout.addWidget(self.editor, 1)

    def load(self, project: Project) -> None:
        self.editor.set_text(project.synopsis)

    def save_to(self, project: Project) -> None:
        project.synopsis = self.editor.get_text()


class OutlineTab(QWidget):
    task_requested = Signal(TaskType, str)
    content_changed = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        header_row = QHBoxLayout()
        title_lbl = QLabel("Outline")
        title_lbl.setStyleSheet(f"font-size: 15px; font-weight: 700; color: {COLOR_TEXT};")
        header_row.addWidget(title_lbl)
        header_row.addStretch()

        gen_btn = QPushButton("Generate")
        gen_btn.setObjectName("accent")
        gen_btn.clicked.connect(lambda: self.task_requested.emit(TaskType.GENERATE_OUTLINE, ""))
        header_row.addWidget(gen_btn)

        review_btn = QPushButton("Review")
        review_btn.clicked.connect(lambda: self.task_requested.emit(TaskType.REVIEW_OUTLINE, ""))
        header_row.addWidget(review_btn)

        layout.addLayout(header_row)

        self.editor = MarkdownEditor(
            placeholder=(
                "Your chapter-by-chapter outline will appear here.\n\n"
                "Click 'Generate' to create one from your synopsis, "
                "or write it manually.\n\n"
                "Format:\n## Chapter 1: Title\nSummary of chapter..."
            )
        )
        self.editor.content_saved.connect(self.content_changed.emit)
        layout.addWidget(self.editor, 1)

    def load(self, project: Project) -> None:
        self.editor.set_text(project.outline)

    def save_to(self, project: Project) -> None:
        project.outline = self.editor.get_text()


class CharacterCard(QFrame):
    edit_requested = Signal(str)  # character id
    delete_requested = Signal(str)

    def __init__(self, character: Character, parent=None) -> None:
        super().__init__(parent)
        self.char_id = character.id
        self.setStyleSheet(f"""
            QFrame {{
                background: {COLOR_SURFACE_RAISED};
                border: 1px solid {COLOR_BORDER};
                border-radius: 8px;
            }}
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(4)

        name_row = QHBoxLayout()
        name_lbl = QLabel(character.name)
        name_lbl.setStyleSheet(f"font-weight: 700; font-size: 14px; color: {COLOR_TEXT}; background: transparent; border: none;")
        name_row.addWidget(name_lbl)
        name_row.addStretch()

        role_lbl = QLabel(character.role)
        role_lbl.setStyleSheet(
            f"color: {COLOR_ACCENT}; font-size: 11px; background: {COLOR_SURFACE}; "
            f"border-radius: 3px; padding: 2px 6px; border: none;"
        )
        name_row.addWidget(role_lbl)
        layout.addLayout(name_row)

        if character.description:
            desc = QLabel(character.description[:120] + ("…" if len(character.description) > 120 else ""))
            desc.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 12px; background: transparent; border: none;")
            desc.setWordWrap(True)
            layout.addWidget(desc)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 4, 0, 0)
        btn_row.addStretch()
        edit_btn = QPushButton("Edit")
        edit_btn.setObjectName("subtle")
        edit_btn.setFixedWidth(48)
        edit_btn.clicked.connect(lambda: self.edit_requested.emit(self.char_id))
        btn_row.addWidget(edit_btn)
        del_btn = QPushButton("✕")
        del_btn.setObjectName("subtle")
        del_btn.setFixedWidth(28)
        del_btn.setStyleSheet(f"QPushButton#subtle {{ color: {COLOR_ERROR}; padding: 0; }}")
        del_btn.clicked.connect(lambda: self.delete_requested.emit(self.char_id))
        btn_row.addWidget(del_btn)
        layout.addLayout(btn_row)


class CharactersTab(QWidget):
    project_changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._project: Optional[Project] = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(10)

        header_row = QHBoxLayout()
        title_lbl = QLabel("Characters")
        title_lbl.setStyleSheet(f"font-size: 15px; font-weight: 700; color: {COLOR_TEXT};")
        header_row.addWidget(title_lbl)
        header_row.addStretch()
        add_btn = QPushButton("+ Add Character")
        add_btn.setObjectName("accent")
        add_btn.clicked.connect(self._add_character)
        header_row.addWidget(add_btn)
        outer.addLayout(header_row)

        # Scrollable cards area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; }")

        self.cards_widget = QWidget()
        self.cards_layout = QVBoxLayout(self.cards_widget)
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        self.cards_layout.setSpacing(8)
        self.cards_layout.addStretch()

        scroll.setWidget(self.cards_widget)
        outer.addWidget(scroll, 1)

    def load(self, project: Project) -> None:
        self._project = project
        self._refresh_cards()

    def _refresh_cards(self) -> None:
        # Remove all cards
        while self.cards_layout.count() > 1:
            item = self.cards_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not self._project:
            return

        for char in self._project.characters:
            card = CharacterCard(char)
            card.edit_requested.connect(self._edit_character)
            card.delete_requested.connect(self._delete_character)
            self.cards_layout.insertWidget(self.cards_layout.count() - 1, card)

        if not self._project.characters:
            empty = QLabel("No characters yet. Click '+ Add Character' to begin.")
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; padding: 30px;")
            self.cards_layout.insertWidget(0, empty)

    def _add_character(self) -> None:
        if not self._project:
            return
        dialog = CharacterDialog(parent=self)
        if dialog.exec() == QDialog.Accepted:
            char = dialog.get_character()
            self._project.characters.append(char)
            storage.save_project(self._project)
            self._refresh_cards()
            self.project_changed.emit()

    def _edit_character(self, char_id: str) -> None:
        if not self._project:
            return
        char = next((c for c in self._project.characters if c.id == char_id), None)
        if not char:
            return
        dialog = CharacterDialog(character=char, parent=self)
        if dialog.exec() == QDialog.Accepted:
            updated = dialog.get_character()
            char.name = updated.name
            char.role = updated.role
            char.description = updated.description
            char.backstory = updated.backstory
            char.traits = updated.traits
            storage.save_project(self._project)
            self._refresh_cards()
            self.project_changed.emit()

    def _delete_character(self, char_id: str) -> None:
        if not self._project:
            return
        char = next((c for c in self._project.characters if c.id == char_id), None)
        if not char:
            return
        reply = QMessageBox.question(
            self, "Delete Character",
            f"Delete '{char.name}'?",
            QMessageBox.Yes | QMessageBox.Cancel,
        )
        if reply == QMessageBox.Yes:
            self._project.characters = [c for c in self._project.characters if c.id != char_id]
            storage.save_project(self._project)
            self._refresh_cards()
            self.project_changed.emit()


class CharacterDialog(QDialog):
    def __init__(self, character: Optional[Character] = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit Character" if character else "New Character")
        self.setMinimumWidth(480)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(20, 20, 20, 20)

        form = QFormLayout()
        form.setSpacing(8)

        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("Character name")
        form.addRow("Name", self.name_input)

        self.role_input = QLineEdit()
        self.role_input.setPlaceholderText("protagonist / antagonist / supporting…")
        form.addRow("Role", self.role_input)

        self.desc_input = QTextEdit()
        self.desc_input.setPlaceholderText("Physical appearance, personality, voice…")
        self.desc_input.setFixedHeight(80)
        form.addRow("Description", self.desc_input)

        self.backstory_input = QTextEdit()
        self.backstory_input.setPlaceholderText("History, motivations, secrets…")
        self.backstory_input.setFixedHeight(80)
        form.addRow("Backstory", self.backstory_input)

        self.traits_input = QLineEdit()
        self.traits_input.setPlaceholderText("Comma-separated: brave, sarcastic, loyal…")
        form.addRow("Traits", self.traits_input)

        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        if character:
            self._char_id = character.id
            self.name_input.setText(character.name)
            self.role_input.setText(character.role)
            self.desc_input.setPlainText(character.description)
            self.backstory_input.setPlainText(character.backstory)
            self.traits_input.setText(", ".join(character.traits))
        else:
            self._char_id = None

    def get_character(self) -> Character:
        import uuid
        traits_raw = self.traits_input.text()
        traits = [t.strip() for t in traits_raw.split(",") if t.strip()]
        return Character(
            id=self._char_id or str(uuid.uuid4()),
            name=self.name_input.text().strip(),
            role=self.role_input.text().strip(),
            description=self.desc_input.toPlainText().strip(),
            backstory=self.backstory_input.toPlainText().strip(),
            traits=traits,
        )

    def accept(self) -> None:
        if not self.name_input.text().strip():
            QMessageBox.warning(self, "Name Required", "Character name is required.")
            return
        super().accept()


class ChapterListItem(QListWidgetItem):
    def __init__(self, chapter: Chapter) -> None:
        super().__init__()
        self.chapter_number = chapter.number
        self._update(chapter)

    def _update(self, chapter: Chapter) -> None:
        reviewed = "✓" if chapter.reviewed else "○"
        has_content = "●" if chapter.content else "·"
        self.setText(f"{has_content} Ch {chapter.number}: {chapter.title} {reviewed}")
        self.setData(Qt.UserRole, chapter.number)


class ChaptersTab(QWidget):
    task_requested = Signal(TaskType, str)
    project_changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._project: Optional[Project] = None
        self._current_chapter: Optional[Chapter] = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        splitter = QSplitter(Qt.Horizontal)

        # Left: chapter list
        left = QWidget()
        left.setMinimumWidth(200)
        left.setMaximumWidth(280)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(12, 12, 8, 12)
        left_layout.setSpacing(8)

        ch_header = QHBoxLayout()
        ch_lbl = QLabel("Chapters")
        ch_lbl.setStyleSheet(f"font-weight: 700; font-size: 14px; color: {COLOR_TEXT};")
        ch_header.addWidget(ch_lbl)
        ch_header.addStretch()
        add_ch_btn = QPushButton("+")
        add_ch_btn.setObjectName("accent")
        add_ch_btn.setFixedSize(24, 24)
        add_ch_btn.setToolTip("Add chapter manually")
        add_ch_btn.setStyleSheet(
            "QPushButton#accent { padding: 0; font-size: 16px; font-weight: 700; }"
        )
        add_ch_btn.clicked.connect(self._add_chapter)
        ch_header.addWidget(add_ch_btn)
        left_layout.addLayout(ch_header)

        self.chapter_list = QListWidget()
        self.chapter_list.currentRowChanged.connect(self._on_chapter_selected)
        left_layout.addWidget(self.chapter_list, 1)

        splitter.addWidget(left)

        # Right: chapter editor
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 12, 12, 12)
        right_layout.setSpacing(8)

        # Chapter title row
        title_row = QHBoxLayout()
        self.chapter_title_edit = QLineEdit()
        self.chapter_title_edit.setPlaceholderText("Chapter title")
        self.chapter_title_edit.setStyleSheet(
            f"font-size: 16px; font-weight: 600; color: {COLOR_TEXT}; "
            f"background: transparent; border: none; border-bottom: 1px solid {COLOR_BORDER}; border-radius: 0; padding: 4px 0;"
        )
        title_row.addWidget(self.chapter_title_edit, 1)
        self.chapter_title_edit.editingFinished.connect(self._save_chapter_title)
        right_layout.addLayout(title_row)

        # Chapter content editor
        self.chapter_editor = QTextEdit()
        self.chapter_editor.setPlaceholderText(
            "Chapter content will appear here.\n\n"
            "Select a chapter from the list and use the action buttons to generate or edit it."
        )
        self.chapter_editor.setStyleSheet(f"""
            QTextEdit {{
                background: {COLOR_SURFACE};
                border: 1px solid {COLOR_BORDER};
                border-radius: 8px;
                padding: 16px;
                font-size: 15px;
                color: {COLOR_TEXT};
                font-family: {FONT_SERIF};
                line-height: 1.7;
            }}
            QTextEdit:focus {{ border-color: {COLOR_ACCENT}; }}
        """)
        right_layout.addWidget(self.chapter_editor, 1)

        # Action buttons
        action_row = QHBoxLayout()
        action_row.setSpacing(8)

        self.write_btn = QPushButton("✍ Write Chapter")
        self.write_btn.setObjectName("accent")
        self.write_btn.clicked.connect(self._write_chapter)
        action_row.addWidget(self.write_btn)

        self.write_book_btn = QPushButton("📚 Write Book")
        self.write_book_btn.clicked.connect(self._write_book)
        action_row.addWidget(self.write_book_btn)

        self.review_btn = QPushButton("Review")
        self.review_btn.clicked.connect(self._review_chapter)
        action_row.addWidget(self.review_btn)

        self.rewrite_btn = QPushButton("↻ Rewrite with Feedback")
        self.rewrite_btn.setToolTip(
            "Rewrites this chapter using the feedback from the last Review. "
            "Run Review first."
        )
        self.rewrite_btn.clicked.connect(self._rewrite_chapter)
        action_row.addWidget(self.rewrite_btn)

        self.memory_btn = QPushButton("Update Memory")
        self.memory_btn.clicked.connect(self._update_memory)
        action_row.addWidget(self.memory_btn)

        action_row.addStretch()

        self.save_ch_btn = QPushButton("Save")
        self.save_ch_btn.clicked.connect(self._save_chapter_content)
        action_row.addWidget(self.save_ch_btn)

        self.delete_ch_btn = QPushButton("Delete")
        self.delete_ch_btn.setObjectName("danger")
        self.delete_ch_btn.clicked.connect(self._delete_chapter)
        action_row.addWidget(self.delete_ch_btn)

        right_layout.addLayout(action_row)
        splitter.addWidget(right)
        splitter.setSizes([220, 700])

        layout.addWidget(splitter)

    def load(self, project: Project) -> None:
        self._project = project
        self._refresh_list()

    def _refresh_list(self) -> None:
        self.chapter_list.clear()
        if not self._project:
            return
        for ch in sorted(self._project.chapters, key=lambda c: c.number):
            self.chapter_list.addItem(ChapterListItem(ch))

    def _on_chapter_selected(self, row: int) -> None:
        if not self._project or row < 0:
            return
        item = self.chapter_list.item(row)
        if not item:
            return
        num = item.data(Qt.UserRole)
        chapter = next((c for c in self._project.chapters if c.number == num), None)
        if chapter:
            self._current_chapter = chapter
            self.chapter_title_edit.setText(chapter.title)
            self.chapter_editor.setPlainText(chapter.content)

    def _save_chapter_title(self) -> None:
        if self._current_chapter:
            self._current_chapter.title = self.chapter_title_edit.text().strip()
            if self._project:
                storage.save_project(self._project)
            self._refresh_list()

    def _save_chapter_content(self) -> None:
        if self._current_chapter:
            self._current_chapter.content = self.chapter_editor.toPlainText()
            if self._project:
                storage.save_project(self._project)
            self._refresh_list()
            self.project_changed.emit()

    def _add_chapter(self) -> None:
        if not self._project:
            return
        next_num = max((c.number for c in self._project.chapters), default=0) + 1
        title, ok = QInputDialog.getText(
            self, "New Chapter", f"Title for Chapter {next_num}:", text=f"Chapter {next_num}"
        )
        if ok and title.strip():
            ch = Chapter(number=next_num, title=title.strip())
            self._project.chapters.append(ch)
            storage.save_project(self._project)
            self._refresh_list()
            self.project_changed.emit()

    def _write_chapter(self) -> None:
        """
        Always writes the NEXT chapter in the story — regardless of which
        chapter happens to be selected/open in the editor. Previously this
        used the *selected* chapter's number when one was open, which meant
        that after writing Chapter 1 (which then stayed selected), clicking
        "Write Chapter" again silently regenerated Chapter 1 instead of
        moving on to Chapter 2.
        """
        if not self._project:
            return
        next_num = max((c.number for c in self._project.chapters), default=0) + 1

        self._project.current_chapter = next_num - 1  # worker adds 1
        self.task_requested.emit(TaskType.WRITE_CHAPTER, "")

    def _write_book(self) -> None:
        if not self._project:
            return
        self.task_requested.emit(TaskType.WRITE_BOOK, "")

    def _review_chapter(self) -> None:
        if not self._current_chapter or not self._project:
            return
        self._project.current_chapter = self._current_chapter.number
        self.task_requested.emit(TaskType.REVIEW_CHAPTER, "")

    def _rewrite_chapter(self) -> None:
        if not self._current_chapter or not self._project:
            return
        if not self._current_chapter.last_review.strip():
            QMessageBox.information(
                self,
                "No Review Yet",
                "Click \"Review\" first to get feedback on this chapter, "
                "then \"Rewrite with Feedback\" to apply it.",
            )
            return
        self._project.current_chapter = self._current_chapter.number
        self.task_requested.emit(TaskType.REWRITE_CHAPTER, "")

    def _update_memory(self) -> None:
        if not self._current_chapter or not self._project:
            return
        self._project.current_chapter = self._current_chapter.number
        self.task_requested.emit(TaskType.UPDATE_MEMORY, "")

    def _delete_chapter(self) -> None:
        if not self._current_chapter or not self._project:
            return
        reply = QMessageBox.question(
            self, "Delete Chapter",
            f"Delete Chapter {self._current_chapter.number}: '{self._current_chapter.title}'?",
            QMessageBox.Yes | QMessageBox.Cancel,
        )
        if reply == QMessageBox.Yes:
            self._project.chapters = [
                c for c in self._project.chapters if c.number != self._current_chapter.number
            ]
            storage.save_project(self._project)
            self._current_chapter = None
            self.chapter_title_edit.clear()
            self.chapter_editor.clear()
            self._refresh_list()
            self.project_changed.emit()

    def refresh_after_generation(self, project: Project) -> None:
        """Called when a task finishes to reload chapter content."""
        self._project = project
        current_num = self._current_chapter.number if self._current_chapter else None
        self._refresh_list()
        if current_num:
            chapter = next((c for c in project.chapters if c.number == current_num), None)
            if chapter:
                self._current_chapter = chapter
                self.chapter_title_edit.setText(chapter.title)
                self.chapter_editor.setPlainText(chapter.content)


class WorldTab(QWidget):
    task_requested = Signal(TaskType, str)
    content_changed = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        header = SectionHeader("World & Setting", "Generate with AI")
        if header.action_btn:
            header.action_btn.setToolTip(
                "Adds to your existing world notes — doesn't overwrite them."
            )
            header.action_btn.clicked.connect(
                lambda: self.task_requested.emit(TaskType.GENERATE_WORLD, "")
            )
        layout.addWidget(header)

        self.editor = MarkdownEditor(
            placeholder=(
                "Describe your story world:\n\n"
                "- Geography and locations\n"
                "- Time period and technology\n"
                "- Magic or special rules\n"
                "- Political structures\n"
                "- Culture and customs\n"
                "- History relevant to the story"
            )
        )
        self.editor.content_saved.connect(self.content_changed.emit)
        layout.addWidget(self.editor, 1)

    def load(self, project: Project) -> None:
        self.editor.set_text(project.world)

    def save_to(self, project: Project) -> None:
        project.world = self.editor.get_text()


class MemoryTab(QWidget):
    content_changed = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        header_row = QHBoxLayout()
        title_lbl = QLabel("Story Memory")
        title_lbl.setStyleSheet(f"font-size: 15px; font-weight: 700; color: {COLOR_TEXT};")
        header_row.addWidget(title_lbl)
        header_row.addStretch()
        info_lbl = QLabel("Automatically updated after each chapter")
        info_lbl.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 12px;")
        header_row.addWidget(info_lbl)
        layout.addLayout(header_row)

        self.editor = MarkdownEditor(
            placeholder=(
                "Story memory is updated automatically after each chapter.\n\n"
                "It tracks:\n"
                "- Characters and their states\n"
                "- Plot events that occurred\n"
                "- World details established\n"
                "- Open threads and foreshadowing"
            )
        )
        self.editor.content_saved.connect(self.content_changed.emit)
        layout.addWidget(self.editor, 1)

    def load(self, project: Project) -> None:
        self.editor.set_text(project.memory)

    def save_to(self, project: Project) -> None:
        project.memory = self.editor.get_text()


class StoryPanel(QWidget):
    """
    Main story panel containing all story-related tabs.
    Emits task_requested(task, extra_input) for the chat panel to execute.
    """

    task_requested = Signal(TaskType, str)
    project_changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._project: Optional[Project] = None
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.tabs = SizeAdjustingTabWidget()
        self.tabs.setDocumentMode(True)

        self.synopsis_tab = SynopsisTab()
        self.synopsis_tab.task_requested.connect(self.task_requested)
        self.synopsis_tab.content_changed.connect(self._on_synopsis_changed)
        self.tabs.addTab(self.synopsis_tab, "Synopsis")

        self.outline_tab = OutlineTab()
        self.outline_tab.task_requested.connect(self.task_requested)
        self.outline_tab.content_changed.connect(self._on_outline_changed)
        self.tabs.addTab(self.outline_tab, "Outline")

        self.chars_tab = CharactersTab()
        self.chars_tab.project_changed.connect(self.project_changed)
        self.tabs.addTab(self.chars_tab, "Characters")

        self.world_tab = WorldTab()
        self.world_tab.task_requested.connect(self.task_requested)
        self.world_tab.content_changed.connect(self._on_world_changed)
        self.tabs.addTab(self.world_tab, "World")

        self.chapters_tab = ChaptersTab()
        self.chapters_tab.task_requested.connect(self.task_requested)
        self.chapters_tab.project_changed.connect(self.project_changed)
        self.tabs.addTab(self.chapters_tab, "Chapters")

        self.memory_tab = MemoryTab()
        self.memory_tab.content_changed.connect(self._on_memory_changed)
        self.tabs.addTab(self.memory_tab, "Memory")

        layout.addWidget(self.tabs)

    def load_project(self, project: Project) -> None:
        self._project = project
        self.synopsis_tab.load(project)
        self.outline_tab.load(project)
        self.chars_tab.load(project)
        self.world_tab.load(project)
        self.chapters_tab.load(project)
        self.memory_tab.load(project)

    def refresh_after_task(self, project: Project) -> None:
        """Called after an AI task finishes to refresh content."""
        self._project = project
        self.synopsis_tab.load(project)
        self.outline_tab.load(project)
        self.chars_tab.load(project)
        self.world_tab.load(project)
        self.memory_tab.load(project)
        self.chapters_tab.refresh_after_generation(project)

    def _save_project(self) -> None:
        if self._project:
            storage.save_project(self._project)
            self.project_changed.emit()

    def _on_synopsis_changed(self, text: str) -> None:
        if self._project:
            self._project.synopsis = text
            self._save_project()

    def _on_outline_changed(self, text: str) -> None:
        if self._project:
            self._project.outline = text
            self._save_project()

    def _on_world_changed(self, text: str) -> None:
        if self._project:
            self._project.world = text
            self._save_project()

    def _on_memory_changed(self, text: str) -> None:
        if self._project:
            self._project.memory = text
            self._save_project()
