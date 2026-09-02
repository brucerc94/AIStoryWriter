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

import html
import math
import re
from typing import Optional

from PySide6.QtCore import (
    QEasingCurve,
    QObject,
    QPropertyAnimation,
    Qt,
    QThread,
    QTimer,
    Signal,
)
from PySide6.QtGui import QFont, QPixmap, QTextCursor, QTextDocument, QTextOption
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGraphicsOpacityEffect,
    QGroupBox,
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
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from engine import storage
from engine.image_workflow import generate_character_image
from engine.models import AuthorIntent, Chapter, Character, CharacterRelationship, Project, TaskType, WritingStyle
from engine.workflow import OUTLINE_EXTEND_MARKER, OUTLINE_SUGGESTION_MARKER
from ui.styles import (
    COLOR_ACCENT,
    COLOR_ACCENT_DIM,
    COLOR_BORDER,
    COLOR_ERROR,
    COLOR_SURFACE,
    COLOR_SURFACE_RAISED,
    COLOR_TEXT,
    COLOR_TEXT_DIM,
    COLOR_TEXT_MUTED,
    FONT_SERIF,
)
from ui.widgets import EmptyStateCard, SizeAdjustingTabWidget

# Imported here (not at top) to avoid circular imports — stats.py itself
# imports outline_chapter_numbers from this module.
from ui.stats import StatsTab
from ui.search import SearchTab


def outline_chapter_numbers(outline_text: str) -> list[int]:
    """
    Chapter numbers found in an outline's "## Chapter N" headings.
    Mirrors engine.workflow's own parser so the UI (chapter-count badge,
    Write Book confirmation) always agrees with what the workflow will
    actually do with the same text.
    """
    numbers = set()
    for match in re.finditer(r"^\s*##\s*Chapter\s+(\d+)\b", outline_text or "", re.IGNORECASE | re.MULTILINE):
        try:
            numbers.add(int(match.group(1)))
        except ValueError:
            continue
    return sorted(numbers)


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
    """
    A labeled text editor for markdown content with a save/generate button row.

    Optionally shows a friendlier empty state (icon + explanation + a
    primary "Generate …" action, plus a "write it myself" link) in place
    of the editor when there's no content yet — pass empty_title to
    enable it. Without empty_title, behaves exactly as before: a plain
    text box with placeholder text.
    """

    content_saved = Signal(str)
    generate_requested = Signal()

    def __init__(
        self,
        placeholder: str = "",
        font_serif: bool = False,
        empty_icon: str = "",
        empty_title: str = "",
        empty_description: str = "",
        generate_label: str = "",
        manual_label: str = "Write it myself",
        parent=None,
    ) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Editor page (always built — this is the "real" content view) ──
        edit_page = QWidget()
        layout = QVBoxLayout(edit_page)
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

        # ── Optional empty state, shown instead of the editor when blank ──
        self._stack: Optional[QStackedWidget] = None
        if empty_title:
            self._stack = QStackedWidget()
            empty_card = EmptyStateCard(
                icon=empty_icon,
                title=empty_title,
                description=empty_description,
                primary_label=generate_label,
                secondary_label=manual_label,
            )
            empty_card.primary_clicked.connect(self.generate_requested)
            empty_card.secondary_clicked.connect(self._show_editor_page)
            self._stack.addWidget(empty_card)   # index 0
            self._stack.addWidget(edit_page)    # index 1
            outer.addWidget(self._stack)
        else:
            outer.addWidget(edit_page)

    def _show_editor_page(self) -> None:
        if self._stack is not None:
            self._stack.setCurrentIndex(1)
            self.editor.setFocus()

    def set_text(self, text: str) -> None:
        self.editor.setPlainText(text)
        if self._stack is not None:
            self._stack.setCurrentIndex(1 if text.strip() else 0)

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

        self._gen_header = SectionHeader("Synopsis", "✨ Generate Synopsis")
        header = self._gen_header
        if header.action_btn:
            header.action_btn.clicked.connect(
                lambda: self.task_requested.emit(TaskType.WRITE_SYNOPSIS, "")
            )
        layout.addWidget(header)

        self.editor = MarkdownEditor(
            placeholder=(
                "Write your story synopsis here, or use 'Generate Synopsis' to create one.\n\n"
                "A good synopsis covers: premise, main characters, central conflict, and stakes."
            ),
            empty_icon="📝",
            empty_title="Every story starts with a synopsis",
            empty_description=(
                "A short premise, main characters, central conflict, and stakes. "
                "The AI can draft one for you to refine, or you can write it yourself."
            ),
            generate_label="✨ Generate Synopsis",
        )
        self.editor.generate_requested.connect(
            lambda: self.task_requested.emit(TaskType.WRITE_SYNOPSIS, "")
        )
        self.editor.content_saved.connect(self.content_changed.emit)
        layout.addWidget(self.editor, 1)

    def load(self, project: Project) -> None:
        self.editor.set_text(project.synopsis)

    def save_to(self, project: Project) -> None:
        project.synopsis = self.editor.get_text()

    def set_busy(self, busy: bool, project_name: str = "") -> None:
        """Disable/enable the Generate button while a task is running."""
        if self._gen_header.action_btn:
            self._gen_header.action_btn.setEnabled(not busy)
            if busy and project_name:
                self._gen_header.action_btn.setToolTip(
                    f"Generating content for \"{project_name}\"…"
                )
            else:
                self._gen_header.action_btn.setToolTip("")
        self.editor.save_btn.setEnabled(not busy)



# ── Style option tables ───────────────────────────────────────────────────────
# Each table is a list of (internal_key, labels_by_language_prefix) tuples.
# internal_key: the value stored in WritingStyle and sent to the model (English).
# labels_by_language_prefix: maps a lowercased language prefix to a display label.
# The first entry in each table is always the "auto" sentinel (key = "").

def _localize(key: str, lang: str, table: list[tuple[str, dict[str, str]]]) -> str:
    """Return the display label for key in lang, falling back to English."""
    lang_lc = lang.lower()[:2] if lang else "en"
    for k, labels in table:
        if k == key:
            return labels.get(lang_lc) or labels.get("en", key)
    return key


_AUTO_LABELS: dict[str, str] = {
    "en": "— auto (infer from synopsis) —",
    "es": "— auto (inferir de la sinopsis) —",
    "fr": "— auto (inférer du synopsis) —",
    "de": "— auto (aus dem Synopsis ableiten) —",
    "pt": "— auto (inferir da sinopse) —",
    "it": "— auto (inferire dalla sinossi) —",
}

_POV_TABLE: list[tuple[str, dict[str, str]]] = [
    ("", _AUTO_LABELS),
    ("First person", {"en": "First person", "es": "Primera persona", "fr": "Première personne", "de": "Erste Person", "pt": "Primeira pessoa", "it": "Prima persona"}),
    ("Third person limited", {"en": "Third person limited", "es": "Tercera persona limitada", "fr": "Troisième personne limitée", "de": "Dritte Person (beschränkt)", "pt": "Terceira pessoa limitada", "it": "Terza persona limitata"}),
    ("Third person omniscient", {"en": "Third person omniscient", "es": "Tercera persona omnisciente", "fr": "Troisième personne omnisciente", "de": "Dritte Person (allwissend)", "pt": "Terceira pessoa onisciente", "it": "Terza persona onnisciente"}),
    ("Second person", {"en": "Second person", "es": "Segunda persona", "fr": "Deuxième personne", "de": "Zweite Person", "pt": "Segunda pessoa", "it": "Seconda persona"}),
    ("Multiple POVs", {"en": "Multiple POVs", "es": "Múltiples puntos de vista", "fr": "Points de vue multiples", "de": "Mehrere Perspektiven", "pt": "Múltiplos pontos de vista", "it": "Più punti di vista"}),
]

_PACING_TABLE: list[tuple[str, dict[str, str]]] = [
    ("", {k: v.replace("(infer from synopsis) ", "") for k, v in _AUTO_LABELS.items()}),
    ("Fast", {"en": "Fast", "es": "Rápido", "fr": "Rapide", "de": "Schnell", "pt": "Rápido", "it": "Veloce"}),
    ("Moderate", {"en": "Moderate", "es": "Moderado", "fr": "Modéré", "de": "Moderat", "pt": "Moderado", "it": "Moderato"}),
    ("Slow", {"en": "Slow", "es": "Lento", "fr": "Lent", "de": "Langsam", "pt": "Lento", "it": "Lento"}),
    ("Variable (fast action / slow reflection)", {"en": "Variable (action/reflection)", "es": "Variable (acción/reflexión)", "fr": "Variable (action/réflexion)", "de": "Variabel (Aktion/Reflexion)", "pt": "Variável (ação/reflexão)", "it": "Variabile (azione/riflessione)"}),
]

_DENSITY_TABLE: list[tuple[str, dict[str, str]]] = [
    ("", {k: v.replace("(infer from synopsis) ", "") for k, v in _AUTO_LABELS.items()}),
    ("Sparse (lean prose, minimal description)", {"en": "Sparse (lean prose)", "es": "Escasa (prosa concisa)", "fr": "Sobre (prose concise)", "de": "Sparsam (knappe Prosa)", "pt": "Esparsa (prosa concisa)", "it": "Scarsa (prosa concisa)"}),
    ("Balanced", {"en": "Balanced", "es": "Equilibrada", "fr": "Équilibrée", "de": "Ausgewogen", "pt": "Equilibrada", "it": "Equilibrata"}),
    ("Rich (detailed description, immersive)", {"en": "Rich (detailed, immersive)", "es": "Rica (detallada, inmersiva)", "fr": "Riche (détaillée, immersive)", "de": "Reich (detailliert, immersiv)", "pt": "Rica (detalhada, imersiva)", "it": "Ricca (dettagliata, immersiva)"}),
]

_DIALOGUE_TABLE: list[tuple[str, dict[str, str]]] = [
    ("", {k: v.replace("(infer from synopsis) ", "") for k, v in _AUTO_LABELS.items()}),
    ("Frequent", {"en": "Frequent", "es": "Frecuente", "fr": "Fréquent", "de": "Häufig", "pt": "Frequente", "it": "Frequente"}),
    ("Moderate", {"en": "Moderate", "es": "Moderado", "fr": "Modéré", "de": "Moderat", "pt": "Moderado", "it": "Moderato"}),
    ("Minimal", {"en": "Minimal", "es": "Mínimo", "fr": "Minimal", "de": "Minimal", "pt": "Mínimo", "it": "Minimale"}),
]

_VIOLENCE_TABLE: list[tuple[str, dict[str, str]]] = [
    ("", {k: v.replace("(infer from synopsis) ", "") for k, v in _AUTO_LABELS.items()}),
    ("None", {"en": "None", "es": "Ninguna", "fr": "Aucune", "de": "Keine", "pt": "Nenhuma", "it": "Nessuna"}),
    ("Implied (off-screen)", {"en": "Implied (off-screen)", "es": "Implícita (fuera de escena)", "fr": "Implicite (hors-écran)", "de": "Angedeutet (außerhalb)", "pt": "Implícita (fora de cena)", "it": "Implicita (fuori scena)"}),
    ("Moderate (present but not graphic)", {"en": "Moderate (not graphic)", "es": "Moderada (no explícita)", "fr": "Modérée (non graphique)", "de": "Moderat (nicht grafisch)", "pt": "Moderada (não gráfica)", "it": "Moderata (non grafica)"}),
    ("Explicit", {"en": "Explicit", "es": "Explícita", "fr": "Explicite", "de": "Explizit", "pt": "Explícita", "it": "Esplicita"}),
]

_ROMANCE_TABLE: list[tuple[str, dict[str, str]]] = [
    ("", {k: v.replace("(infer from synopsis) ", "") for k, v in _AUTO_LABELS.items()}),
    ("None", {"en": "None", "es": "Ninguno", "fr": "Aucun", "de": "Kein", "pt": "Nenhum", "it": "Nessuno"}),
    ("Background (minor thread)", {"en": "Background (minor thread)", "es": "Secundario (hilo menor)", "fr": "Secondaire (fil mineur)", "de": "Hintergrund (kleiner Strang)", "pt": "Secundário (fio menor)", "it": "Sfondo (filo minore)"}),
    ("Subplot", {"en": "Subplot", "es": "Subtrama", "fr": "Intrigue secondaire", "de": "Nebenhandlung", "pt": "Subtrama", "it": "Sottotrama"}),
    ("Central (primary storyline)", {"en": "Central (primary)", "es": "Central (trama principal)", "fr": "Central (intrigue principale)", "de": "Zentral (Haupthandlung)", "pt": "Central (trama principal)", "it": "Centrale (trama principale)"}),
]

_LENGTH_TABLE: list[tuple[str, dict[str, str]]] = [
    ("", {k: v.replace("(infer from synopsis) ", "") for k, v in _AUTO_LABELS.items()}),
    ("Short (~1k words)", {"en": "Short (~1 000 words)", "es": "Corto (~1 000 palabras)", "fr": "Court (~1 000 mots)", "de": "Kurz (~1 000 Wörter)", "pt": "Curto (~1 000 palavras)", "it": "Corto (~1 000 parole)"}),
    ("Medium (~2k words)", {"en": "Medium (~2 000 words)", "es": "Medio (~2 000 palabras)", "fr": "Moyen (~2 000 mots)", "de": "Mittel (~2 000 Wörter)", "pt": "Médio (~2 000 palavras)", "it": "Medio (~2 000 parole)"}),
    ("Long (~3k+ words)", {"en": "Long (~3 000+ words)", "es": "Largo (~3 000+ palabras)", "fr": "Long (~3 000+ mots)", "de": "Lang (~3 000+ Wörter)", "pt": "Longo (~3 000+ palavras)", "it": "Lungo (~3 000+ parole)"}),
]

# Also: localized labels for UI form rows and group boxes (used by both dialog and panel)
_UI_STRINGS: dict[str, dict[str, str]] = {
    "structure_group":    {"en": "Structure", "es": "Estructura", "fr": "Structure", "de": "Struktur", "pt": "Estrutura", "it": "Struttura"},
    "num_chapters":       {"en": "Number of chapters", "es": "Número de capítulos", "fr": "Nombre de chapitres", "de": "Kapitelanzahl", "pt": "Número de capítulos", "it": "Numero di capitoli"},
    "intent_group":       {"en": "Author's Creative Intent  (optional — leave blank to infer from synopsis)", "es": "Intención creativa del autor  (opcional — dejar en blanco para inferir de la sinopsis)", "fr": "Intention créative de l'auteur  (optionnel)", "de": "Kreative Absicht des Autors  (optional)", "pt": "Intenção criativa do autor  (opcional)", "it": "Intenzione creativa dell'autore  (opzionale)"},
    "style_group":        {"en": "Writing Style Preferences  (optional — leave at '— auto —' to infer)", "es": "Preferencias de estilo de escritura  (opcional — dejar en '— auto —')", "fr": "Préférences de style  (optionnel)", "de": "Schreibstil-Einstellungen  (optional)", "pt": "Preferências de estilo  (opcional)", "it": "Preferenze di stile  (opzionale)"},
    "emotional_journey":  {"en": "Emotional journey\nfor the reader", "es": "Arco emocional\npara el lector", "fr": "Voyage émotionnel\ndu lecteur", "de": "Emotionale Reise\ndes Lesers", "pt": "Jornada emocional\ndo leitor", "it": "Viaggio emotivo\ndel lettore"},
    "lasting_impression": {"en": "What readers remember\nafter finishing", "es": "Lo que el lector recordará\nal terminar", "fr": "Ce que le lecteur retient\naprès lecture", "de": "Was der Leser behält\nnach dem Lesen", "pt": "O que o leitor lembrará\nao terminar", "it": "Cosa ricorderà il lettore\ndopo la lettura"},
    "themes":             {"en": "Core themes to explore", "es": "Temas centrales a explorar", "fr": "Thèmes centraux à explorer", "de": "Kernthemen", "pt": "Temas centrais a explorar", "it": "Temi centrali da esplorare"},
    "unique_elements":    {"en": "What makes it unique", "es": "Qué lo hace único", "fr": "Ce qui le rend unique", "de": "Was es einzigartig macht", "pt": "O que o torna único", "it": "Cosa lo rende unico"},
    "inspirations":       {"en": "Inspirations (and what\naspect)", "es": "Inspiraciones (y en qué\naspecto)", "fr": "Inspirations (et quel\naspect)", "de": "Inspirationen (und welcher\nAspekt)", "pt": "Inspirações (e em qual\naspecto)", "it": "Ispirazioni (e quale\naspetto)"},
    "avoid":              {"en": "Avoid entirely", "es": "Evitar completamente", "fr": "Éviter entièrement", "de": "Vollständig vermeiden", "pt": "Evitar completamente", "it": "Evitare completamente"},
    "genre_tags":         {"en": "Genre (if ambiguous)", "es": "Género (si es ambiguo)", "fr": "Genre (si ambigu)", "de": "Genre (falls mehrdeutig)", "pt": "Gênero (se ambíguo)", "it": "Genere (se ambiguo)"},
    "narrator_pov":       {"en": "Narrative POV", "es": "Punto de vista narrativo", "fr": "Point de vue narratif", "de": "Erzählperspektive", "pt": "Ponto de vista narrativo", "it": "Punto di vista narrativo"},
    "pacing":             {"en": "Pacing", "es": "Ritmo", "fr": "Rythme", "de": "Erzähltempo", "pt": "Ritmo", "it": "Ritmo"},
    "description_density":{"en": "Description density", "es": "Densidad descriptiva", "fr": "Densité descriptive", "de": "Beschreibungsdichte", "pt": "Densidade descritiva", "it": "Densità descrittiva"},
    "dialogue_style":     {"en": "Dialogue", "es": "Diálogo", "fr": "Dialogue", "de": "Dialog", "pt": "Diálogo", "it": "Dialogo"},
    "violence_level":     {"en": "Violence", "es": "Violencia", "fr": "Violence", "de": "Gewalt", "pt": "Violência", "it": "Violenza"},
    "romance_level":      {"en": "Romance", "es": "Romance", "fr": "Romance", "de": "Romantik", "pt": "Romance", "it": "Romanticismo"},
    "chapter_length":     {"en": "Chapter length", "es": "Longitud de capítulo", "fr": "Longueur des chapitres", "de": "Kapitellänge", "pt": "Comprimento do capítulo", "it": "Lunghezza del capitolo"},
    "genre_placeholder":  {"en": "Only if ambiguous from synopsis — e.g. psychological thriller, neo-noir", "es": "Solo si es ambiguo en la sinopsis — p. ej. thriller psicológico, neo-noir", "fr": "Seulement si ambigu dans le synopsis", "de": "Nur wenn im Synopsis unklar", "pt": "Somente se ambíguo na sinopse", "it": "Solo se ambiguo nella sinossi"},
    "generate_btn":       {"en": "Generate Outline", "es": "Generar estructura", "fr": "Générer le plan", "de": "Gliederung generieren", "pt": "Gerar estrutura", "it": "Genera struttura"},
    "save_profile_btn":   {"en": "Save Profile", "es": "Guardar perfil", "fr": "Enregistrer le profil", "de": "Profil speichern", "pt": "Salvar perfil", "it": "Salva profilo"},
    "load_profile_btn":   {"en": "Load Profile...", "es": "Cargar perfil...", "fr": "Charger le profil...", "de": "Profil laden...", "pt": "Carregar perfil...", "it": "Carica profilo..."},
    "load_profile_dialog_title": {"en": "Load Profile From Project", "es": "Cargar perfil de otro proyecto", "fr": "Charger le profil d'un autre projet", "de": "Profil aus anderem Projekt laden", "pt": "Carregar perfil de outro projeto", "it": "Carica profilo da un altro progetto"},
    "load_profile_dialog_hint": {"en": "Pick a project to copy its Creative Intent and Writing Style from. Nothing is saved until you click \"Save Profile\".", "es": "Elegí un proyecto para copiar su Intención creativa y Estilo de escritura. No se guarda nada hasta que hagas clic en \"Guardar perfil\".", "fr": "Choisissez un projet dont copier l'intention créative et le style d'écriture. Rien n'est enregistré avant de cliquer sur \"Enregistrer le profil\".", "de": "Wähle ein Projekt, um dessen kreative Absicht und Schreibstil zu kopieren. Es wird nichts gespeichert, bis du auf \"Profil speichern\" klickst.", "pt": "Escolha um projeto para copiar sua Intenção criativa e Estilo de escrita. Nada é salvo até você clicar em \"Salvar perfil\".", "it": "Scegli un progetto da cui copiare l'Intenzione creativa e lo Stile di scrittura. Non viene salvato nulla finché non fai clic su \"Salva profilo\"."},
    "load_profile_no_other_projects": {"en": "No other projects with a saved profile were found.", "es": "No se encontraron otros proyectos con un perfil guardado.", "fr": "Aucun autre projet avec un profil enregistré n'a été trouvé.", "de": "Es wurden keine anderen Projekte mit gespeichertem Profil gefunden.", "pt": "Nenhum outro projeto com um perfil salvo foi encontrado.", "it": "Non è stato trovato nessun altro progetto con un profilo salvato."},
    "load_profile_loaded_status": {"en": "Profile loaded from \"{title}\" — review it, then click Save Profile.", "es": "Perfil cargado desde \"{title}\" — revisalo y hacé clic en Guardar perfil.", "fr": "Profil chargé depuis \"{title}\" — vérifiez-le, puis cliquez sur Enregistrer le profil.", "de": "Profil aus \"{title}\" geladen — prüfen und dann auf Profil speichern klicken.", "pt": "Perfil carregado de \"{title}\" — revise e clique em Salvar perfil.", "it": "Profilo caricato da \"{title}\" — controllalo, poi clicca su Salva profilo."},
    "profile_tab_hint":   {"en": "Used by Outline · Write Chapter · Rewrite · Review", "es": "Usado por Estructura · Escribir capítulo · Reescribir · Revisión", "fr": "Utilisé par : Plan · Écrire · Réécrire · Relire", "de": "Genutzt von: Gliederung · Kapitel · Neufassung · Review", "pt": "Usado por: Estrutura · Capítulo · Reescrever · Revisão", "it": "Usato da: Schema · Capitolo · Riscrittura · Revisione"},
    "intent_group_short": {"en": "Creative Intent", "es": "Intención creativa", "fr": "Intention créative", "de": "Kreative Absicht", "pt": "Intenção criativa", "it": "Intenzione creativa"},
    "style_group_short":  {"en": "Writing Style  (leave at '— auto —' to let the model infer)", "es": "Estilo de escritura  (dejar en '— auto —' para que el modelo infiera)", "fr": "Style d'écriture  (laisser '— auto —' pour inférer)", "de": "Schreibstil  ('— auto —' lassen zum Ableiten)", "pt": "Estilo de escrita  (deixar '— auto —' para o modelo inferir)", "it": "Stile di scrittura  (lasciare '— auto —' per inferire)"},
}


def _ui(key: str, lang: str) -> str:
    """Resolve a UI string key to the user's language, fallback to English."""
    lang_lc = lang.lower()[:2] if lang else "en"
    row = _UI_STRINGS.get(key, {})
    return row.get(lang_lc) or row.get("en", key)


def _make_combo(table: list[tuple[str, dict[str, str]]], lang: str, parent=None) -> QComboBox:
    """Build a QComboBox from a key/label table.
    itemData(i) holds the internal English key; itemText(i) is the localized label.
    """
    cb = QComboBox(parent)
    lang_lc = lang.lower()[:2] if lang else "en"
    for key, labels in table:
        label = labels.get(lang_lc) or labels.get("en", key) or "— auto —"
        cb.addItem(label, key)
    return cb


def _combo_value(combo: QComboBox) -> str:
    """Return the internal key stored as itemData, or '' for the auto sentinel."""
    return combo.currentData() or ""


def _set_combo(combo: QComboBox, value: str) -> None:
    """Select the item whose itemData matches value; stay on index 0 if not found."""
    if not value:
        return
    for i in range(combo.count()):
        if combo.itemData(i) == value:
            combo.setCurrentIndex(i)
            return



class GenerateOutlineDialog(QDialog):
    """
    Outline generation wizard.

    Collects:
      • Chapter count  (structural — required)
      • Author intent  (creative goals — all optional, pre-filled from project)
      • Writing style  (technical preferences — all optional, pre-filled)

    Combo boxes display labels in the configured response language; their
    itemData() always holds the internal English key that gets stored in
    WritingStyle and sent to the model.  Fields left at '— auto —' are
    omitted from the prompt; the model infers them from the synopsis.

    "Generate Full Book" later writes exactly one chapter per outline heading, so
    the chapter count set here determines the length of the finished novel.
    """

    # Keep these as class-level references so AuthorProfilePanel can reuse them.
    _POV_TABLE    = _POV_TABLE
    _PACING_TABLE = _PACING_TABLE
    _DENSITY_TABLE = _DENSITY_TABLE
    _DIALOGUE_TABLE = _DIALOGUE_TABLE
    _VIOLENCE_TABLE = _VIOLENCE_TABLE
    _ROMANCE_TABLE  = _ROMANCE_TABLE
    _LENGTH_TABLE   = _LENGTH_TABLE

    def __init__(
        self,
        default_chapters: int = 12,
        author_intent: Optional[AuthorIntent] = None,
        writing_style: Optional[WritingStyle] = None,
        lang: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(_ui("generate_btn", lang))
        self.setMinimumWidth(560)
        self.setMinimumHeight(640)
        self.setModal(True)
        self._lang = lang

        intent = author_intent or AuthorIntent()
        style = writing_style or WritingStyle()

        root = QVBoxLayout(self)
        root.setSpacing(12)
        root.setContentsMargins(20, 20, 20, 20)

        # ── Scrollable body ───────────────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; }")
        body_widget = QWidget()
        body = QVBoxLayout(body_widget)
        body.setSpacing(14)
        body.setContentsMargins(0, 0, 8, 0)
        scroll.setWidget(body_widget)
        root.addWidget(scroll, 1)

        # ── Section 1: Structure ──────────────────────────────────────
        struct_box = QGroupBox(_ui("structure_group", lang))
        struct_form = QFormLayout(struct_box)
        struct_form.setSpacing(8)
        struct_form.setContentsMargins(14, 18, 14, 14)

        self.chapters_spin = QSpinBox()
        self.chapters_spin.setRange(1, 300)
        self.chapters_spin.setValue(max(1, default_chapters))
        self.chapters_spin.setToolTip(
            "The outline — and later \"Generate Full Book\" — will follow this number, "
            "one chapter per heading."
        )
        struct_form.addRow(_ui("num_chapters", lang), self.chapters_spin)
        body.addWidget(struct_box)

        # ── Section 2: Author's Creative Intent ───────────────────────
        intent_box = QGroupBox(_ui("intent_group", lang))
        intent_form = QFormLayout(intent_box)
        intent_form.setSpacing(10)
        intent_form.setContentsMargins(14, 18, 14, 14)

        def _intent_edit(placeholder: str, value: str) -> QTextEdit:
            w = QTextEdit()
            w.setPlaceholderText(placeholder)
            w.setPlainText(value)
            w.setFixedHeight(56)
            w.setStyleSheet(
                f"QTextEdit {{ font-size: 12px; padding: 4px 8px; "
                f"background: {COLOR_SURFACE}; border: 1px solid {COLOR_BORDER}; "
                f"border-radius: 4px; color: {COLOR_TEXT}; }}"
                f"QTextEdit:focus {{ border-color: {COLOR_ACCENT}; }}"
            )
            return w

        self.emotional_journey = _intent_edit(
            "e.g. Growing unease that never fully resolves into catharsis",
            intent.emotional_journey,
        )
        intent_form.addRow(_ui("emotional_journey", lang), self.emotional_journey)

        self.lasting_impression = _intent_edit(
            "e.g. The normalcy of cruelty when systems protect it",
            intent.lasting_impression,
        )
        intent_form.addRow(_ui("lasting_impression", lang), self.lasting_impression)

        self.themes = _intent_edit(
            "e.g. Institutional complicity, the cost of silence, identity under pressure",
            intent.themes,
        )
        intent_form.addRow(_ui("themes", lang), self.themes)

        self.unique_elements = _intent_edit(
            "e.g. Unreliable narrator revealed gradually; non-linear structure mirrors memory",
            intent.unique_elements,
        )
        intent_form.addRow(_ui("unique_elements", lang), self.unique_elements)

        self.inspirations = _intent_edit(
            "e.g. Kazuo Ishiguro's restraint; Flynn's narrative misdirection",
            intent.inspirations,
        )
        intent_form.addRow(_ui("inspirations", lang), self.inspirations)

        self.avoid = _intent_edit(
            "e.g. Redemption arcs, graphic gore, comic relief, romantic subplots",
            intent.avoid,
        )
        intent_form.addRow(_ui("avoid", lang), self.avoid)

        body.addWidget(intent_box)

        # ── Section 3: Writing Style ──────────────────────────────────
        style_box = QGroupBox(_ui("style_group", lang))
        style_form = QFormLayout(style_box)
        style_form.setSpacing(8)
        style_form.setContentsMargins(14, 18, 14, 14)

        self.genre_tags = QLineEdit()
        self.genre_tags.setPlaceholderText(_ui("genre_placeholder", lang))
        self.genre_tags.setText(style.genre_tags)
        self.genre_tags.setMaxLength(120)
        style_form.addRow(_ui("genre_tags", lang), self.genre_tags)

        self.narrator_pov = _make_combo(_POV_TABLE, lang)
        _set_combo(self.narrator_pov, style.narrator_pov)
        style_form.addRow(_ui("narrator_pov", lang), self.narrator_pov)

        self.pacing = _make_combo(_PACING_TABLE, lang)
        _set_combo(self.pacing, style.pacing)
        style_form.addRow(_ui("pacing", lang), self.pacing)

        self.description_density = _make_combo(_DENSITY_TABLE, lang)
        _set_combo(self.description_density, style.description_density)
        style_form.addRow(_ui("description_density", lang), self.description_density)

        self.dialogue_style = _make_combo(_DIALOGUE_TABLE, lang)
        _set_combo(self.dialogue_style, style.dialogue_style)
        style_form.addRow(_ui("dialogue_style", lang), self.dialogue_style)

        self.violence_level = _make_combo(_VIOLENCE_TABLE, lang)
        _set_combo(self.violence_level, style.violence_level)
        style_form.addRow(_ui("violence_level", lang), self.violence_level)

        self.romance_level = _make_combo(_ROMANCE_TABLE, lang)
        _set_combo(self.romance_level, style.romance_level)
        style_form.addRow(_ui("romance_level", lang), self.romance_level)

        self.target_chapter_length = _make_combo(_LENGTH_TABLE, lang)
        _set_combo(self.target_chapter_length, style.target_chapter_length)
        style_form.addRow(_ui("chapter_length", lang), self.target_chapter_length)

        body.addWidget(style_box)

        # ── Buttons ───────────────────────────────────────────────────
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText(_ui("generate_btn", lang))
        buttons.button(QDialogButtonBox.Ok).setObjectName("accent")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # ── Public accessors ──────────────────────────────────────────────

    def chapter_count(self) -> int:
        return self.chapters_spin.value()

    def get_author_intent(self) -> AuthorIntent:
        """Return an AuthorIntent populated from the dialog's intent fields."""
        return AuthorIntent(
            emotional_journey=self.emotional_journey.toPlainText().strip(),
            lasting_impression=self.lasting_impression.toPlainText().strip(),
            themes=self.themes.toPlainText().strip(),
            unique_elements=self.unique_elements.toPlainText().strip(),
            inspirations=self.inspirations.toPlainText().strip(),
            avoid=self.avoid.toPlainText().strip(),
        )

    def get_writing_style(self) -> WritingStyle:
        """Return a WritingStyle from the dialog's combo selections.
        itemData() holds the internal English key; empty string = auto.
        """
        return WritingStyle(
            genre_tags=self.genre_tags.text().strip(),
            narrator_pov=_combo_value(self.narrator_pov),
            pacing=_combo_value(self.pacing),
            description_density=_combo_value(self.description_density),
            dialogue_style=_combo_value(self.dialogue_style),
            violence_level=_combo_value(self.violence_level),
            romance_level=_combo_value(self.romance_level),
            target_chapter_length=_combo_value(self.target_chapter_length),
        )


class ExtendOutlineDialog(QDialog):
    """
    Extend-outline dialog.

    Mirrors GenerateOutlineDialog's structural pattern (a QGroupBox holding a
    QFormLayout, with a QSpinBox for the chapter count) so the number of new
    chapters is a first-class, explicit, user-set constraint — never left
    solely to the model's discretion — plus a free-text field describing
    what the new chapters should cover.
    """

    def __init__(
        self,
        default_new_chapters: int = 5,
        lang: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Extend Outline")
        self.setMinimumWidth(480)
        self.setModal(True)
        self._lang = lang

        root = QVBoxLayout(self)
        root.setSpacing(12)
        root.setContentsMargins(20, 20, 20, 20)

        # ── Section: Structure ────────────────────────────────────────
        struct_box = QGroupBox("Structure")
        struct_form = QFormLayout(struct_box)
        struct_form.setSpacing(8)
        struct_form.setContentsMargins(14, 18, 14, 14)

        self.chapters_spin = QSpinBox()
        self.chapters_spin.setRange(1, 100)
        self.chapters_spin.setValue(max(1, default_new_chapters))
        self.chapters_spin.setToolTip(
            "Exactly this many new chapters will be generated and appended "
            "after the current last chapter."
        )
        struct_form.addRow("Number of chapters to add", self.chapters_spin)
        root.addWidget(struct_box)

        # ── Section: What to cover ─────────────────────────────────────
        cover_label = QLabel("What would you like these chapters to cover?")
        root.addWidget(cover_label)

        self.cover_text = QTextEdit()
        self.cover_text.setPlaceholderText(
            "e.g. The protagonist's confrontation with the antagonist and its aftermath"
        )
        self.cover_text.setMinimumHeight(120)
        self.cover_text.setStyleSheet(
            f"QTextEdit {{ font-size: 12px; padding: 4px 8px; "
            f"background: {COLOR_SURFACE}; border: 1px solid {COLOR_BORDER}; "
            f"border-radius: 4px; color: {COLOR_TEXT}; }}"
            f"QTextEdit:focus {{ border-color: {COLOR_ACCENT}; }}"
        )
        root.addWidget(self.cover_text, 1)

        # ── Buttons ───────────────────────────────────────────────────
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Extend Outline")
        buttons.button(QDialogButtonBox.Ok).setObjectName("accent")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # ── Public accessors ──────────────────────────────────────────────

    def chapter_count(self) -> int:
        return self.chapters_spin.value()

    def cover_request(self) -> str:
        return self.cover_text.toPlainText().strip()


class OutlineTab(QWidget):
    task_requested = Signal(TaskType, str)
    content_changed = Signal(str)
    # Emitted after the user confirms the dialog so MainWindow can persist
    # the updated AuthorIntent and WritingStyle before the task starts.
    profile_updated = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._project: Optional[Project] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        header_row = QHBoxLayout()
        title_lbl = QLabel("Outline")
        title_lbl.setStyleSheet(f"font-size: 15px; font-weight: 700; color: {COLOR_TEXT};")
        header_row.addWidget(title_lbl)

        self.count_lbl = QLabel("")
        self.count_lbl.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 11px; padding-left: 8px;")
        header_row.addWidget(self.count_lbl)
        header_row.addStretch()

        self._gen_btn = QPushButton("✨ Generate Outline")
        gen_btn = self._gen_btn
        gen_btn.setObjectName("accent")
        gen_btn.setToolTip("Opens the outline wizard, then generates the full outline.")
        gen_btn.clicked.connect(self._on_generate_clicked)
        header_row.addWidget(gen_btn)

        self._extend_btn = QPushButton("➕ Extend Outline")
        self._extend_btn.setObjectName("subtle")
        self._extend_btn.setToolTip(
            "Add new chapters onto the end of the existing outline without changing it."
        )
        self._extend_btn.clicked.connect(self._on_extend_outline_clicked)
        header_row.addWidget(self._extend_btn)

        layout.addLayout(header_row)

        self.editor = MarkdownEditor(
            placeholder=(
                "Your chapter-by-chapter outline will appear here.\n\n"
                "Click 'Generate Outline' to create one from your synopsis, "
                "or write it manually.\n\n"
                "Format:\n## Chapter 1: Title\nObjective:\n...\n\nStory Progression:\n...\n\nContinuity:\n..."
            ),
            empty_icon="🗂️",
            empty_title="No outline yet",
            empty_description=(
                "The AI will build a chapter-by-chapter outline from your Synopsis, "
                "Characters, and World — or you can write your own below."
            ),
            generate_label="✨ Generate Outline",
        )
        self.editor.generate_requested.connect(self._on_generate_clicked)
        self.editor.editor.textChanged.connect(self._update_count_label)
        self.editor.content_saved.connect(self.content_changed.emit)
        layout.addWidget(self.editor, 1)

    def _on_generate_clicked(self) -> None:
        existing = outline_chapter_numbers(self.editor.get_text())
        default_n = max(existing) if existing else 12

        # Read the configured response language so the dialog can localize
        # its labels and combo options for the author.
        lang = storage.load_settings().response_language

        # Pre-fill the wizard with whatever the project already has saved.
        current_intent = self._project.author_intent if self._project else None
        current_style = self._project.writing_style if self._project else None

        dialog = GenerateOutlineDialog(
            default_chapters=default_n,
            author_intent=current_intent,
            writing_style=current_style,
            lang=lang,
            parent=self,
        )
        if dialog.exec() != QDialog.Accepted:
            return

        # Persist the new intent/style back into the project object so the
        # worker can read them from project.author_intent / project.writing_style.
        if self._project is not None:
            self._project.author_intent = dialog.get_author_intent()
            self._project.writing_style = dialog.get_writing_style()
            self.profile_updated.emit()

        n = dialog.chapter_count()
        # Build the requirement string the worker appends to its base_prompt.
        # The intent/style now travel via project fields — only the chapter
        # count (a structural constraint) goes in the requirement string.
        requirement = (
            f"The outline must contain EXACTLY {n} chapters, "
            f"numbered sequentially from 1 to {n}."
        )
        self.task_requested.emit(TaskType.GENERATE_OUTLINE, requirement)

    def _on_regenerate_with_suggestion_clicked(self) -> None:
        if not self._project or not self.editor.get_text().strip():
            QMessageBox.information(
                self,
                "No outline yet",
                "Generate or write an outline first.",
            )
            return

        # Persist unsaved editor text before the workflow starts.
        self.content_changed.emit(self.editor.get_text())

        suggestion, accepted = QInputDialog.getMultiLineText(
            self,
            "Regenerate Outline with Suggestion",
            "What would you like to change in the existing outline?",
            "",
        )
        if not accepted or not suggestion.strip():
            return

        self.task_requested.emit(
            TaskType.GENERATE_OUTLINE,
            f"{OUTLINE_SUGGESTION_MARKER}\n{suggestion.strip()}",
        )

    def _on_extend_outline_clicked(self) -> None:
        if not self._project or not self.editor.get_text().strip():
            QMessageBox.information(
                self,
                "No outline yet",
                "Generate an outline first, then you can extend it.",
            )
            return

        # Persist unsaved editor text before the workflow starts.
        self.content_changed.emit(self.editor.get_text())

        lang = storage.load_settings().response_language
        dialog = ExtendOutlineDialog(default_new_chapters=5, lang=lang, parent=self)
        if dialog.exec() != QDialog.Accepted:
            return

        request = dialog.cover_request()
        if not request:
            return
        n = dialog.chapter_count()

        self.task_requested.emit(
            TaskType.GENERATE_OUTLINE,
            f"{OUTLINE_EXTEND_MARKER}\n{n}\n{request}",
        )

    def _update_count_label(self) -> None:
        numbers = outline_chapter_numbers(self.editor.get_text())
        if not numbers:
            self.count_lbl.setText("")
            return
        n = len(numbers)
        max_n = max(numbers)
        label = f"· {n} chapter{'s' if n != 1 else ''} planned"
        if max_n != n:
            label += f" (numbered up to {max_n})"
        self.count_lbl.setText(label)

    def load(self, project: Project) -> None:
        self._project = project
        self.editor.set_text(project.outline)
        self._update_count_label()

    def set_busy(self, busy: bool, project_name: str = "") -> None:
        tip = f"Generating content for \"{project_name}\"…" if busy and project_name else ""
        self._gen_btn.setEnabled(not busy)
        self._extend_btn.setEnabled(not busy)
        if busy:
            self._gen_btn.setToolTip(tip)
            self._extend_btn.setToolTip(tip)
        else:
            self._gen_btn.setToolTip("Opens the outline wizard, then generates the full outline.")
            self._extend_btn.setToolTip(
                "Add new chapters onto the end of the existing outline without changing it."
            )

    def save_to(self, project: Project) -> None:
        project.outline = self.editor.get_text()


class _ClickableImageLabel(QLabel):
    """A QLabel that emits a clicked signal when the user presses it."""
    clicked = Signal()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class CharacterCard(QFrame):
    edit_requested = Signal(str)  # character id
    delete_requested = Signal(str)
    image_requested = Signal(str, bool)  # character id, regenerate

    def __init__(self, character: Character, project_id: str = "", parent=None) -> None:
        super().__init__(parent)
        self.char_id = character.id
        self._project_id_value = project_id
        self._current_pixmap: Optional[QPixmap] = None
        self._character_name = character.name
        self.setStyleSheet(f"""
            QFrame {{
                background: {COLOR_SURFACE_RAISED};
                border: 1px solid {COLOR_BORDER};
                border-radius: 8px;
            }}
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)

        header_row = QHBoxLayout()
        title_col = QVBoxLayout()
        name_lbl = QLabel(character.name)
        name_lbl.setStyleSheet(f"font-weight: 700; font-size: 14px; color: {COLOR_TEXT}; background: transparent; border: none;")
        title_col.addWidget(name_lbl)

        role_lbl = QLabel(character.role or "")
        role_lbl.setStyleSheet(
            f"color: {COLOR_ACCENT}; font-size: 11px; background: {COLOR_SURFACE}; "
            f"border-radius: 3px; padding: 2px 6px; border: none;"
        )
        title_col.addWidget(role_lbl)
        header_row.addLayout(title_col, 1)

        self.image_status_lbl = QLabel(character.image_status or "No Image")
        self.image_status_lbl.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 11px; background: transparent; border: none;")
        header_row.addWidget(self.image_status_lbl)
        layout.addLayout(header_row)

        self.image_preview = _ClickableImageLabel()
        self.image_preview.setFixedSize(96, 96)
        self.image_preview.setStyleSheet(f"border: 1px solid {COLOR_BORDER}; border-radius: 6px; background: {COLOR_SURFACE};")
        self.image_preview.setAlignment(Qt.AlignCenter)
        self.image_preview.clicked.connect(self._show_image_preview)
        self._set_preview(character)
        layout.addWidget(self.image_preview, 0, Qt.AlignLeft)

        if character.description:
            desc = QLabel(character.description[:120] + ("…" if len(character.description) > 120 else ""))
            desc.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 12px; background: transparent; border: none;")
            desc.setWordWrap(True)
            layout.addWidget(desc)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 4, 0, 0)
        btn_row.addStretch()
        image_btn = QPushButton("Generate Image" if not character.image_ref else "Regenerate Image")
        image_btn.setObjectName("accent")
        image_btn.setFixedWidth(90)
        image_btn.clicked.connect(lambda: self.image_requested.emit(self.char_id, bool(character.image_ref)))
        btn_row.addWidget(image_btn)
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

    def _set_preview(self, character: Character) -> None:
        self._current_pixmap = None
        if not character.image_ref:
            self.image_preview.setText("🖼️")
            self.image_preview.setStyleSheet(f"border: 1px dashed {COLOR_BORDER}; border-radius: 6px; background: {COLOR_SURFACE}; font-size: 28px;")
            self.image_preview.setCursor(Qt.ArrowCursor)
            self.image_preview.setToolTip("")
            return
        try:
            data = storage.load_binary_resource(self._project_id_value, character.image_ref)
            if data:
                pixmap = QPixmap()
                pixmap.loadFromData(data)
                if not pixmap.isNull():
                    self._current_pixmap = pixmap
                    self.image_preview.setPixmap(pixmap.scaled(96, 96, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                    self.image_preview.setStyleSheet(
                        f"border: 1px solid {COLOR_ACCENT}; border-radius: 6px; background: {COLOR_SURFACE};"
                    )
                    self.image_preview.setCursor(Qt.PointingHandCursor)
                    self.image_preview.setToolTip("Click to view full image")
                    return
        except Exception:
            pass
        self.image_preview.setText("🖼️")
        self.image_preview.setStyleSheet(f"border: 1px dashed {COLOR_BORDER}; border-radius: 6px; background: {COLOR_SURFACE}; font-size: 28px;")
        self.image_preview.setCursor(Qt.ArrowCursor)
        self.image_preview.setToolTip("")

    def _show_image_preview(self) -> None:
        if self._current_pixmap is None or self._current_pixmap.isNull():
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(f"{self._character_name} — Portrait")
        dialog.setModal(True)
        vbox = QVBoxLayout(dialog)
        vbox.setContentsMargins(16, 16, 16, 16)
        vbox.setSpacing(12)

        img_lbl = QLabel()
        img_lbl.setAlignment(Qt.AlignCenter)
        max_size = 512
        scaled = self._current_pixmap.scaled(max_size, max_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        img_lbl.setPixmap(scaled)
        vbox.addWidget(img_lbl)

        close_btn = QPushButton("Close")
        close_btn.setObjectName("accent")
        close_btn.clicked.connect(dialog.accept)
        vbox.addWidget(close_btn, 0, Qt.AlignCenter)

        dialog.exec()

    def _project_id(self) -> str:
        return self._project_id_value


class _CharacterImageWorker(QObject):
    finished = Signal(object)

    def __init__(self, project_id: str, character: Character, settings) -> None:
        super().__init__()
        self.project_id = project_id
        self.character = character
        self.settings = settings

    def run(self) -> None:
        try:
            ok, image_ref, error = generate_character_image(self.project_id, self.character, self.settings)
        except Exception as exc:  # pragma: no cover - defensive path
            ok, image_ref, error = False, None, str(exc)
        self.finished.emit((ok, image_ref, error))


class CharactersTab(QWidget):
    project_changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._project: Optional[Project] = None
        self._settings = storage.load_settings()
        self._image_thread: Optional[QThread] = None
        self._image_worker: Optional[_CharacterImageWorker] = None

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

    def _project_id(self) -> str:
        return self._project.id if self._project else ""

    def _refresh_cards(self) -> None:
        # Remove all cards
        while self.cards_layout.count() > 1:
            item = self.cards_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not self._project:
            return

        for char in self._project.characters:
            card = CharacterCard(char, project_id=self._project.id)
            card.edit_requested.connect(self._edit_character)
            card.delete_requested.connect(self._delete_character)
            card.image_requested.connect(self._handle_character_image)
            self.cards_layout.insertWidget(self.cards_layout.count() - 1, card)

        if not self._project.characters:
            empty = EmptyStateCard(
                icon="🎭",
                title="No characters yet",
                description=(
                    "Add the people driving your story — protagonists, antagonists, "
                    "and everyone in between. The outline and chapters will draw on these."
                ),
                primary_label="+ Create Character",
            )
            empty.primary_clicked.connect(self._add_character)
            self.cards_layout.insertWidget(0, empty)

    def _add_character(self) -> None:
        if not self._project:
            return
        dialog = CharacterDialog(all_characters=self._project.characters, parent=self)
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
        dialog = CharacterDialog(character=char, all_characters=self._project.characters, parent=self)
        if dialog.exec() == QDialog.Accepted:
            updated = dialog.get_character()
            char.name = updated.name
            char.role = updated.role
            char.description = updated.description
            char.backstory = updated.backstory
            char.traits = updated.traits
            char.relationships = updated.relationships
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

    def _handle_character_image(self, char_id: str, regenerate: bool) -> None:
        if not self._project or (self._image_thread and self._image_thread.isRunning()):
            return
        char = next((c for c in self._project.characters if c.id == char_id), None)
        if not char:
            return
        char.image_status = "Generating"
        char.image_error = ""
        storage.save_project(self._project)
        self._refresh_cards()

        self._image_worker = _CharacterImageWorker(self._project.id, char, self._settings)
        self._image_thread = QThread(self)
        self._image_worker.moveToThread(self._image_thread)
        self._image_thread.started.connect(self._image_worker.run)
        self._image_worker.finished.connect(self._on_character_image_finished)
        self._image_worker.finished.connect(self._image_thread.quit)
        self._image_thread.finished.connect(self._image_thread.deleteLater)
        self._image_thread.start()

    def _on_character_image_finished(self, payload: tuple[bool, Optional[dict], str]) -> None:
        if not self._project:
            return
        ok, image_ref, error = payload
        char = next((c for c in self._project.characters if c.id == self._image_worker.character.id), None) if self._image_worker else None
        if not char:
            return
        if ok and image_ref:
            char.image_ref = image_ref
            char.image_status = "Ready"
            char.image_error = ""
        else:
            char.image_status = "Error"
            char.image_error = error or "Image generation failed."
        storage.save_project(self._project)
        self._refresh_cards()
        self.project_changed.emit()
        self._image_worker = None
        self._image_thread = None


class CharacterDialog(QDialog):
    # Predefined relationship types (label shown in the combo-box).
    # The user can also type a custom value by selecting "otro".
    _RELATIONSHIP_TYPES = [
        "padre de", "madre de", "hijo de", "hija de",
        "hermano de", "hermana de",
        "abuelo de", "abuela de", "nieto de", "nieta de",
        "tío de", "tía de", "sobrino de", "sobrina de",
        "primo de", "prima de",
        "esposo de", "esposa de", "pareja de", "expareja de",
        "amigo de", "amiga de",
        "enemigo de", "enemiga de",
        "rival de", "mentor de", "alumno de",
        "jefe de", "subordinado de",
        "otro",
    ]

    def __init__(
        self,
        character: Optional[Character] = None,
        all_characters: Optional[list] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit Character" if character else "New Character")
        self.setMinimumWidth(520)
        self.setModal(True)

        # Names of every other character in the project (for relationship targets)
        self._all_character_names: list[str] = [
            c.name for c in (all_characters or [])
            if character is None or c.id != character.id
        ]

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(20, 20, 20, 20)

        # ── Basic fields ──────────────────────────────────────────────────
        form = QFormLayout()
        form.setSpacing(8)

        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("Character name")
        form.addRow("Name", self.name_input)

        self.role_input = QLineEdit()
        self.role_input.setPlaceholderText("protagonist / antagonist / supporting…")
        form.addRow("Role", self.role_input)

        self.desc_input = QTextEdit()
        self.desc_input.setPlaceholderText(
            "Only physical description. Include age, ethnicity or race, overall appearance, and visible traits only. "
            "Example: 'Age: 28. Latin American woman, tall and slim, dark hair, green eyes, warm brown skin, sharp features, black coat, scar on the left cheek.'"
        )
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

        # ── Relationships section ─────────────────────────────────────────
        rel_group = QGroupBox("Relationships")
        rel_layout = QVBoxLayout(rel_group)
        rel_layout.setSpacing(6)

        # Scrollable list of existing relationships
        self._rel_list = QListWidget()
        self._rel_list.setFixedHeight(120)
        rel_layout.addWidget(self._rel_list)

        # Row for adding / editing a relationship
        add_row = QHBoxLayout()
        add_row.setSpacing(6)

        self._rel_target_combo = QComboBox()
        self._rel_target_combo.setEditable(True)
        self._rel_target_combo.setPlaceholderText("Other character…")
        if self._all_character_names:
            self._rel_target_combo.addItems(self._all_character_names)
        else:
            self._rel_target_combo.addItem("")
        self._rel_target_combo.setCurrentIndex(-1)
        self._rel_target_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        add_row.addWidget(self._rel_target_combo)

        self._rel_type_combo = QComboBox()
        self._rel_type_combo.setEditable(True)
        self._rel_type_combo.addItems(self._RELATIONSHIP_TYPES)
        self._rel_type_combo.setCurrentIndex(-1)
        self._rel_type_combo.setPlaceholderText("Relationship type…")
        self._rel_type_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        add_row.addWidget(self._rel_type_combo)

        btn_add_rel = QPushButton("Add")
        btn_add_rel.setFixedWidth(60)
        btn_add_rel.clicked.connect(self._add_relationship)
        add_row.addWidget(btn_add_rel)

        rel_layout.addLayout(add_row)

        # Edit / delete buttons for the selected list item
        edit_del_row = QHBoxLayout()
        edit_del_row.setSpacing(6)
        btn_edit_rel = QPushButton("Edit selected")
        btn_edit_rel.clicked.connect(self._edit_selected_relationship)
        edit_del_row.addWidget(btn_edit_rel)
        btn_del_rel = QPushButton("Delete selected")
        btn_del_rel.clicked.connect(self._delete_selected_relationship)
        edit_del_row.addWidget(btn_del_rel)
        edit_del_row.addStretch()
        rel_layout.addLayout(edit_del_row)

        layout.addWidget(rel_group)

        # ── Dialog buttons ────────────────────────────────────────────────
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        # ── Populate from existing character ──────────────────────────────
        if character:
            self._char_id = character.id
            self._existing_character = character
            self.name_input.setText(character.name)
            self.role_input.setText(character.role)
            self.desc_input.setPlainText(character.description)
            self.backstory_input.setPlainText(character.backstory)
            self.traits_input.setText(", ".join(character.traits))
            for rel in getattr(character, "relationships", []):
                self._append_rel_item(rel.related_character, rel.relationship)
        else:
            self._char_id = None
            self._existing_character = None

    # ── Relationship list helpers ─────────────────────────────────────────

    def _append_rel_item(self, target: str, rel_type: str) -> None:
        """Add one relationship entry to the list widget."""
        item = QListWidgetItem(f"{rel_type}  →  {target}")
        # Store data as a tuple in the user role
        item.setData(Qt.UserRole, (target, rel_type))
        self._rel_list.addItem(item)

    def _add_relationship(self) -> None:
        target = self._rel_target_combo.currentText().strip()
        rel_type = self._rel_type_combo.currentText().strip()
        if not target:
            QMessageBox.warning(self, "Missing target", "Select or type the other character's name.")
            return
        if not rel_type:
            QMessageBox.warning(self, "Missing type", "Select or type the relationship type.")
            return
        # Handle "otro" — ask for a custom label
        if rel_type.lower() == "otro":
            custom, ok = QInputDialog.getText(self, "Custom relationship", "Enter relationship label (e.g. 'rival de'):")
            if not ok or not custom.strip():
                return
            rel_type = custom.strip()
        # Prevent duplicate entries for the same (target, type) pair
        for i in range(self._rel_list.count()):
            stored = self._rel_list.item(i).data(Qt.UserRole)
            if stored and stored[0] == target and stored[1] == rel_type:
                QMessageBox.information(self, "Duplicate", "This relationship already exists.")
                return
        self._append_rel_item(target, rel_type)
        # Reset inputs
        self._rel_target_combo.setCurrentIndex(-1)
        self._rel_type_combo.setCurrentIndex(-1)

    def _edit_selected_relationship(self) -> None:
        item = self._rel_list.currentItem()
        if not item:
            return
        stored = item.data(Qt.UserRole)
        if not stored:
            return
        old_target, old_type = stored
        # Pre-fill the add-row fields so the user can modify and re-add
        if old_target in self._all_character_names:
            self._rel_target_combo.setCurrentText(old_target)
        else:
            self._rel_target_combo.setCurrentText(old_target)
        self._rel_type_combo.setCurrentText(old_type)
        # Remove the old entry so the user "replaces" it via Add
        row = self._rel_list.row(item)
        self._rel_list.takeItem(row)

    def _delete_selected_relationship(self) -> None:
        item = self._rel_list.currentItem()
        if not item:
            return
        row = self._rel_list.row(item)
        self._rel_list.takeItem(row)

    # ── Result extraction ─────────────────────────────────────────────────

    def get_character(self) -> Character:
        import uuid
        traits_raw = self.traits_input.text()
        traits = [t.strip() for t in traits_raw.split(",") if t.strip()]
        existing = getattr(self, "_existing_character", None)

        relationships: list[CharacterRelationship] = []
        for i in range(self._rel_list.count()):
            stored = self._rel_list.item(i).data(Qt.UserRole)
            if stored:
                target, rel_type = stored
                if target and rel_type:
                    relationships.append(CharacterRelationship(
                        related_character=target,
                        relationship=rel_type,
                    ))

        return Character(
            id=self._char_id or str(uuid.uuid4()),
            name=self.name_input.text().strip(),
            role=self.role_input.text().strip(),
            description=self.desc_input.toPlainText().strip(),
            backstory=self.backstory_input.toPlainText().strip(),
            traits=traits,
            image_ref=getattr(existing, "image_ref", None),
            image_status=getattr(existing, "image_status", "No Image"),
            image_error=getattr(existing, "image_error", ""),
            relationships=relationships,
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


class ChangeChapterDialog(QDialog):
    """
    Modal dialog that collects the author's change instructions for a chapter.
    Shows the chapter title for context and provides a multi-line text area
    for the user to describe what they want the AI to change.
    """

    def __init__(self, chapter: Chapter, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Change Chapter {chapter.number}")
        self.setMinimumWidth(480)
        self.setMinimumHeight(300)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        # Header label
        header = QLabel(
            f"<b>Chapter {chapter.number}: {chapter.title}</b><br>"
            "<span style='color:#aaa;font-size:13px;'>"
            "Describe what you want the AI to change in this chapter."
            "</span>"
        )
        header.setTextFormat(Qt.RichText)
        header.setWordWrap(True)
        layout.addWidget(header)

        # Instructions text area
        self._text = QTextEdit()
        self._text.setPlaceholderText(
            "Examples:\n"
            "• Make the dialogue between Ana and Carlos more tense and confrontational.\n"
            "• Add a brief description of the setting at the start of the chapter.\n"
            "• Remove the flashback in the middle — it slows the pacing.\n"
            "• Change the ending so Elena decides to leave instead of stay."
        )
        self._text.setMinimumHeight(160)
        layout.addWidget(self._text)

        # Buttons
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Apply Changes")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def instructions(self) -> str:
        return self._text.toPlainText().strip()

    def _on_accept(self) -> None:
        if not self.instructions():
            QMessageBox.warning(
                self,
                "No Instructions",
                "Please describe what you want to change before clicking Apply.",
            )
            return
        self.accept()


def _escape_book_text(text: str) -> str:
    return html.escape(text, quote=False)


def _split_chapter_paragraphs(content: str) -> list[str]:
    """
    Splits raw chapter text into paragraphs on blank lines, preserving any
    single newlines *within* a paragraph (e.g. short dialogue exchanges)
    as soft line breaks. Purely a presentation transform for the book
    reader — it never reads back into or mutates chapter.content.
    """
    normalized = (content or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    raw = re.split(r"\n\s*\n", normalized)
    return [p.strip("\n") for p in raw if p.strip()]


def _serif_font(point_size: int, bold: bool = False) -> QFont:
    font = QFont()
    families = [f.strip(" '\"") for f in FONT_SERIF.split(",")]
    font.setFamilies(families)
    font.setPointSize(point_size)
    font.setBold(bold)
    return font


def _serif_font_px(pixel_size: int, bold: bool = False) -> QFont:
    """
    Same as _serif_font but sized in device pixels via setPixelSize
    instead of setPointSize. This is what lets the *measuring* document
    match the QTextEdit's CSS `font-size: Npx` exactly — mixing pt-sized
    measurement fonts with a px-sized display stylesheet is what was
    causing the pagination to under-fill each page.
    """
    font = QFont()
    families = [f.strip(" '\"") for f in FONT_SERIF.split(",")]
    font.setFamilies(families)
    font.setPixelSize(pixel_size)
    font.setBold(bold)
    return font


def _render_book_fragment(
    segment: str, is_title: bool, is_continuation: bool, title_font: QFont, spacing_value: int
) -> str:
    """
    Builds the HTML for one already-measured block (or block segment) for
    on-screen rendering. `spacing_value` must be the exact same *rounded*
    integer pixel value that was fed into the height accumulator during
    `_paginate_book_pages` for this same block — using an unrounded float
    here (or a differently-rounded one) would make the rendered page a
    fraction of a pixel taller/shorter than what pagination measured,
    which is exactly the kind of drift that causes clipped text.
    """
    text_html = _escape_book_text(segment).replace("\u2028", "<br>")
    if is_title:
        # font-weight must match the *measuring* QFont's setBold(True)
        # (effectively CSS 700/"bold") rather than an arbitrary 600 —
        # a different weight can resolve to a different font face with
        # different metrics, which would desync measured vs rendered
        # title height.
        return (
            f"<p style='margin:0 0 {spacing_value}px 0; "
            f"font-family:\"{title_font.family()}\"; font-size:{title_font.pixelSize()}px; "
            f"font-weight:bold; letter-spacing:2px; text-align:center;'>{text_html}</p>"
        )
    indent_style = "" if is_continuation else "text-indent:22px;"
    return f"<p style='margin:0 0 {spacing_value}px 0; text-align:justify; {indent_style}'>{text_html}</p>"


def _paginate_book_pages(
    title: str,
    paragraphs: list[str],
    body_font: QFont,
    title_font: QFont,
    page_width: float,
    page_height: float,
    paragraph_spacing: float = 4.0,
    height_safety_px: float = 1.0,
) -> list[str]:
    """
    Lays out the chapter title + paragraphs at the given fonts and page
    width using a QTextDocument, then walks the resulting line boxes to
    slice the flow into pages of at most `page_height`. Breaks only ever
    fall between lines — never mid-word — and a paragraph that doesn't
    fit on one page simply continues, unbroken, onto the next, exactly
    like a printed book. Purely a presentation transform: never reads or
    writes chapter.content, only paginates already-loaded text for
    display.

    This function is the single source of truth for "how much fits on a
    page" — `page_text_edit` is only ever fed pages built here, and it
    must render each one without any vertical scrolling. To guarantee
    that, every measurement here has to agree pixel-for-pixel with how
    the real QTextEdit will lay the same HTML out:

      * `body_font` / `title_font` must be the exact QFont (family +
        pixel size + weight) the QTextEdit's stylesheet resolves to —
        callers build these with `_serif_font_px`, matched to the
        widget's `font-size: Npx` rule.
      * `page_width` / `page_height` must be the real, already-resized
        `page_text_edit.viewport()` width/height, not a value derived
        from outer layout math — margins, borders, or scrollbar policy
        can shave a few px off of what a naive "card size minus padding"
        calculation predicts.
      * the wrap mode set on this throwaway measuring document must
        match the QTextEdit's (both default to
        WrapAtWordBoundaryOrAnywhere, but it's set explicitly on both
        ends so a future change to one can't silently desync from the
        other).
      * spacing values are rounded to whole pixels *once*, here, and
        that same integer is what both the height accumulator and the
        rendered CSS margin use (via `_render_book_fragment`) — mixing
        an unrounded accumulator with rounded CSS output is what
        previously let pages drift out of sync with what they measured.

    `height_safety_px` is a small conservative margin subtracted from
    `page_height` before packing lines. It exists purely as a guard
    against residual sub-pixel differences between two independently
    laid-out QTextDocuments (the throwaway one here and the QTextEdit's
    real one) — it can never make a page overflow, only make a page very
    slightly less full than the absolute theoretical maximum.

    Returns a list of ready-to-render HTML fragments, one per page.
    """
    usable_height = max(page_height - max(height_safety_px, 0.0), 1.0)

    doc = QTextDocument()
    doc.setDocumentMargin(0)
    doc.setDefaultFont(body_font)
    # Explicit wrap mode so this throwaway measuring document can never
    # silently drift from whatever QTextEdit's own default happens to be
    # on a given Qt version — both are pinned to the same value.
    text_option = doc.defaultTextOption()
    text_option.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
    doc.setDefaultTextOption(text_option)
    doc.setTextWidth(max(page_width, 50))

    has_title = bool(title.strip())
    spacings: list[int] = []
    blocks_html: list[str] = []
    if has_title:
        title_spacing = round(paragraph_spacing * 1.8)
        blocks_html.append(
            f"<p style='margin:0 0 {title_spacing}px 0; "
            f"font-family:\"{title_font.family()}\"; font-size:{title_font.pixelSize()}px; "
            f"font-weight:bold; letter-spacing:2px; text-align:center;'>"
            f"{_escape_book_text(title.strip().upper())}</p>"
        )
        spacings.append(title_spacing)
    body_spacing = round(paragraph_spacing)
    for para in paragraphs:
        blocks_html.append(
            f"<p style='margin:0 0 {body_spacing}px 0; text-align:justify; text-indent:22px;'>"
            f"{_escape_book_text(para).replace(chr(10), '<br>')}</p>"
        )
        spacings.append(body_spacing)
    if not blocks_html:
        blocks_html.append("<p>&nbsp;</p>")
        spacings.append(0)

    doc.setHtml("".join(blocks_html))
    doc_layout = doc.documentLayout()

    pages: list[list[str]] = []
    current_page: list[str] = []
    current_height = 0.0
    # Whether the page being built already has any line placed on it.
    # This is deliberately tracked separately from `current_page` (the
    # list of *committed* HTML fragments): a block's lines accumulate
    # into current_height as they're measured, but only get committed to
    # current_page once a break or the block's end is reached. Gating the
    # overflow check on `current_page` instead of this flag meant a
    # block's own earlier lines — measured but not yet committed — never
    # counted as "this page already has content", so a single long block
    # (e.g. the chapter's first paragraph) could never break internally
    # and would silently overflow the page instead of splitting.
    page_has_content = False
    # Spacing owed *before* the next block, if that block ends up sharing
    # this page. Deferred rather than added immediately after each block,
    # so a paragraph that doesn't fit doesn't force its predecessor's
    # trailing gap onto the page that's about to close — that gap was
    # never actually needed once nothing follows it on that page, and
    # counting it anyway used to end pages a few pixels earlier than the
    # real available height allowed.
    pending_spacing = 0.0

    block = doc.firstBlock()
    block_idx = 0
    while block.isValid():
        is_title_block = has_title and block_idx == 0
        spacing_value = spacings[block_idx] if block_idx < len(spacings) else body_spacing
        # QTextDocument lays blocks out lazily — force this block's layout
        # before reading its line boxes, or lineCount() comes back 0 for
        # any block beyond the first handful.
        doc_layout.blockBoundingRect(block)
        layout = block.layout()
        text = block.text()
        line_count = layout.lineCount()
        if line_count == 0:
            block = block.next()
            block_idx += 1
            continue
        seg_start = 0
        # Only the block's very first line (li == 0) can carry the gap
        # owed from the previous block, and only if this page already has
        # content above it (a fresh page never starts with a leading gap).
        carry = pending_spacing if page_has_content else 0.0
        for li in range(line_count):
            line = layout.lineAt(li)
            # Round up: a fractional line height still occupies a full
            # pixel row once painted, so treating it as smaller than it
            # renders is exactly what lets the last line on a page get
            # visually clipped.
            lh = math.ceil(line.height())
            extra = carry if li == 0 else 0.0
            if page_has_content and current_height + extra + lh > usable_height:
                seg = text[seg_start:line.textStart()]
                if seg:
                    current_page.append(
                        _render_book_fragment(seg, is_title_block, seg_start > 0, title_font, 0)
                    )
                pages.append(current_page)
                current_page = []
                current_height = 0.0
                page_has_content = False
                pending_spacing = 0.0
                seg_start = line.textStart()
                extra = 0.0
            current_height += extra + lh
            page_has_content = True
            carry = 0.0
        remainder = text[seg_start:]
        if remainder:
            current_page.append(
                _render_book_fragment(remainder, is_title_block, seg_start > 0, title_font, spacing_value)
            )
        # Don't fold this block's trailing spacing into current_height
        # yet — hold it until we know whether another block will actually
        # land on this same page.
        pending_spacing = spacing_value
        block = block.next()
        block_idx += 1

    if current_page:
        pages.append(current_page)
    if not pages:
        pages = [["<p>&nbsp;</p>"]]

    return ["".join(p) for p in pages]


class _BookPageTextEdit(QTextEdit):
    """
    Fixed presentation viewport for a book page. It must never become a
    vertically scrollable reading surface: pagination is responsible for
    fitting the complete page into the viewport.
    """

    def wheelEvent(self, event) -> None:  # noqa: N802 — Qt override
        event.ignore()

    def scrollContentsBy(self, dx: int, dy: int) -> None:  # noqa: N802 — Qt override
        # Prevent any implicit scrolling after HTML/document relayout.
        return


class _ReadPane(QWidget):
    """
    Thin container for the whole book-reading pane. Exists so page
    navigation can respond to Left/Right/Home/End while the reading pane
    has focus, without interfering with any other widget's key handling
    (the chapter list, the plain-text editor, etc).
    """

    page_key_pressed = Signal(str)  # "prev" | "next" | "first" | "last"

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.StrongFocus)

    def keyPressEvent(self, event) -> None:  # noqa: N802 — Qt override
        key = event.key()
        mapping = {
            Qt.Key_Left: "prev",
            Qt.Key_Right: "next",
            Qt.Key_Home: "first",
            Qt.Key_End: "last",
        }
        action = mapping.get(key)
        if action is None:
            super().keyPressEvent(event)
            return
        self.page_key_pressed.emit(action)
        event.accept()

    def mousePressEvent(self, event) -> None:  # noqa: N802 — Qt override
        self.setFocus()
        super().mousePressEvent(event)


class ChaptersTab(QWidget):
    task_requested = Signal(TaskType, str)
    project_changed = Signal()

    # Paper-page metrics for the book reader (px). Kept as class constants
    # so the pagination math and the widget geometry always agree.
    PAGE_PADDING_H = 56
    PAGE_PADDING_V = 12
    PAGE_NUMBER_RESERVE = 16  # space reserved at the page bottom for the folio number
    BODY_FONT_PX = 14   # must match the QTextEdit's `font-size` in its stylesheet
    TITLE_FONT_PX = 20  # chapter-title size on a page's opening block
    CARD_LAYOUT_SPACING = 0  # gap between the text area and the folio number

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._project: Optional[Project] = None
        self._current_chapter: Optional[Chapter] = None

        # ── Book-reader state ───────────────────────────────────────────
        self._mode = "read"  # "read" | "edit" — which view is showing
        self._pages: list[str] = []  # current chapter's paginated HTML
        self._current_page_index = 0
        self._page_anim: Optional[QPropertyAnimation] = None
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(lambda: self._refresh_read_view(preserve_fraction=True))

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

        # Right: chapter editor (or an empty state when the project has no
        # chapters at all yet — swapped in/out by _update_empty_state()).
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 12, 12, 12)
        right_layout.setSpacing(8)

        self._chapters_empty_card = EmptyStateCard(
            icon="📖",
            title="No chapters yet",
            description=(
                "Generate an outline first, then let the AI draft your opening "
                "chapter from it — or add one manually and write it yourself."
            ),
            primary_label="✍ Generate Chapter",
            secondary_label="+ Add Chapter Manually",
            tertiary_label="📚 Generate Full Book",
        )
        self._chapters_empty_card.primary_clicked.connect(self._write_chapter)
        self._chapters_empty_card.secondary_clicked.connect(self._add_chapter)
        self._chapters_empty_card.tertiary_clicked.connect(self._write_book)
        right_layout.addWidget(self._chapters_empty_card, 1)

        self._editor_area = QWidget()
        editor_area_layout = QVBoxLayout(self._editor_area)
        editor_area_layout.setContentsMargins(0, 0, 0, 0)
        editor_area_layout.setSpacing(8)

        # ── Read / Edit mode toggle ─────────────────────────────────────
        mode_row = QHBoxLayout()
        mode_row.addStretch()
        self.read_mode_btn = QPushButton("📖 Read")
        self.edit_mode_btn = QPushButton("✏ Edit")
        for btn in (self.read_mode_btn, self.edit_mode_btn):
            btn.setCheckable(True)
            btn.setObjectName("modeToggle")
            btn.setStyleSheet(f"""
                QPushButton#modeToggle {{
                    padding: 4px 14px;
                    border: 1px solid {COLOR_BORDER};
                    background: {COLOR_SURFACE};
                    color: {COLOR_TEXT_DIM};
                }}
                QPushButton#modeToggle:checked {{
                    background: {COLOR_ACCENT_DIM};
                    color: {COLOR_TEXT};
                    border-color: {COLOR_ACCENT};
                }}
            """)
        self.read_mode_btn.setChecked(True)
        self.read_mode_btn.clicked.connect(lambda: self._set_mode("read"))
        self.edit_mode_btn.clicked.connect(lambda: self._set_mode("edit"))
        mode_row.addWidget(self.read_mode_btn)
        mode_row.addWidget(self.edit_mode_btn)
        editor_area_layout.addLayout(mode_row)

        self.content_stack = QStackedWidget()
        self._build_read_view()
        self.content_stack.addWidget(self.read_view)  # index 0

        self.edit_view = QWidget()
        editor_layout = QVBoxLayout(self.edit_view)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(8)

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
        editor_layout.addLayout(title_row)

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
        editor_layout.addWidget(self.chapter_editor, 1)

        # ── Word count footer ──────────────────────────────────────────
        # Updates on every keystroke via textChanged. Sits between the
        # editor and the action buttons so it reads as part of the editor,
        # not as a separate control.
        wc_row = QHBoxLayout()
        wc_row.setContentsMargins(2, 0, 2, 0)

        self._wc_label = QLabel("0 words")
        self._wc_label.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font-size: 11px; background: transparent;"
        )
        wc_row.addWidget(self._wc_label)

        wc_row.addStretch()

        self._read_time_label = QLabel("")
        self._read_time_label.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font-size: 11px; background: transparent;"
        )
        wc_row.addWidget(self._read_time_label)

        editor_layout.addLayout(wc_row)

        self.chapter_editor.textChanged.connect(self._update_word_count)
        action_row = QHBoxLayout()
        action_row.setSpacing(8)

        self.write_btn = QPushButton("✍ Generate Chapter")
        self.write_btn.setObjectName("accent")
        self.write_btn.setToolTip("Writes the next chapter in the story.")
        self.write_btn.clicked.connect(self._write_chapter)
        action_row.addWidget(self.write_btn)

        self.write_book_btn = QPushButton("📚 Generate Full Book")
        self.write_book_btn.setToolTip("Writes every remaining chapter in the outline, one after another.")
        self.write_book_btn.clicked.connect(self._write_book)
        action_row.addWidget(self.write_book_btn)

        self.change_btn = QPushButton("✏ Change Chapter")
        self.change_btn.setToolTip(
            "Tell the AI what you want to change in this chapter and it will apply your instructions."
        )
        self.change_btn.clicked.connect(self._change_chapter)
        action_row.addWidget(self.change_btn)

        self.mark_ready_btn = QPushButton("✓ Mark as Ready")
        self.mark_ready_btn.setToolTip("Marks this chapter as reviewed/ready, whether it was written by the AI or by hand.")
        self.mark_ready_btn.clicked.connect(self._mark_chapter_ready)
        action_row.addWidget(self.mark_ready_btn)

        action_row.addStretch()

        self.save_ch_btn = QPushButton("Save")
        self.save_ch_btn.clicked.connect(self._save_chapter_content)
        action_row.addWidget(self.save_ch_btn)

        self.delete_ch_btn = QPushButton("Delete")
        self.delete_ch_btn.setObjectName("danger")
        self.delete_ch_btn.clicked.connect(self._delete_chapter)
        action_row.addWidget(self.delete_ch_btn)

        editor_layout.addLayout(action_row)

        self.content_stack.addWidget(self.edit_view)  # index 1
        editor_area_layout.addWidget(self.content_stack, 1)
        right_layout.addWidget(self._editor_area, 1)

        splitter.addWidget(right)
        splitter.setSizes([220, 700])

        layout.addWidget(splitter)

    def _build_read_view(self) -> None:
        """
        Builds the book-style reading pane: chapter nav header, a single
        paper-like page with paginated text, and a page nav footer. This
        never touches chapter.content — it only ever displays a
        pagination of whatever text is already loaded.
        """
        self.read_view = _ReadPane()
        self.read_view.page_key_pressed.connect(self._on_page_key)
        read_layout = QVBoxLayout(self.read_view)
        read_layout.setContentsMargins(4, 4, 4, 4)
        read_layout.setSpacing(12)

        # ── Chapter nav header ──────────────────────────────────────────
        nav_top = QHBoxLayout()
        self.prev_chapter_btn = QPushButton("← Previous Chapter")
        self.prev_chapter_btn.setFlat(True)
        self.prev_chapter_btn.clicked.connect(self._go_prev_chapter)
        nav_top.addWidget(self.prev_chapter_btn)
        nav_top.addStretch()

        self.chapter_header_lbl = QLabel("")
        self.chapter_header_lbl.setAlignment(Qt.AlignCenter)
        self.chapter_header_lbl.setStyleSheet(
            f"color: {COLOR_TEXT}; font-weight: 700; font-size: 14px; background: transparent;"
        )
        nav_top.addWidget(self.chapter_header_lbl)
        nav_top.addStretch()

        self.next_chapter_btn = QPushButton("Next Chapter →")
        self.next_chapter_btn.setFlat(True)
        self.next_chapter_btn.clicked.connect(self._go_next_chapter)
        nav_top.addWidget(self.next_chapter_btn)
        read_layout.addLayout(nav_top)

        # ── The book page itself ────────────────────────────────────────
        self._page_outer = QWidget()
        page_outer_layout = QVBoxLayout(self._page_outer)
        page_outer_layout.setContentsMargins(0, 0, 0, 0)
        page_outer_layout.setAlignment(Qt.AlignCenter)

        self._page_card = QFrame()
        self._page_card.setObjectName("bookPageCard")
        self._page_card.setStyleSheet(f"""
            QFrame#bookPageCard {{
                background: #f4f0e6;
                border: 1px solid #d8d0bd;
                border-radius: 6px;
            }}
        """)
        self._page_opacity_effect = QGraphicsOpacityEffect(self._page_card)
        self._page_opacity_effect.setOpacity(1.0)
        self._page_card.setGraphicsEffect(self._page_opacity_effect)

        card_layout = QVBoxLayout(self._page_card)
        card_layout.setContentsMargins(
            self.PAGE_PADDING_H, self.PAGE_PADDING_V, self.PAGE_PADDING_H, self.PAGE_PADDING_V
        )
        card_layout.setSpacing(self.CARD_LAYOUT_SPACING)

        self.page_text_edit = _BookPageTextEdit()
        self.page_text_edit.setReadOnly(True)
        self.page_text_edit.setFrameShape(QFrame.NoFrame)
        self.page_text_edit.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.page_text_edit.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.page_text_edit.setTextInteractionFlags(Qt.NoTextInteraction)
        self.page_text_edit.setFocusPolicy(Qt.NoFocus)
        self.page_text_edit.document().setDocumentMargin(0)
        self.page_text_edit.setContentsMargins(0, 0, 0, 0)
        self.page_text_edit.setViewportMargins(0, 0, 0, 0)
        # Pinned explicitly so it can never silently diverge from the
        # wrap mode `_paginate_book_pages` sets on its measuring
        # QTextDocument — both must break lines identically or pagination
        # and rendering disagree on how many lines a paragraph takes.
        self.page_text_edit.setWordWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        self.page_text_edit.setStyleSheet(f"""
            QTextEdit {{
                background: transparent;
                border: none;
                color: #2b2620;
                font-family: {FONT_SERIF};
                font-size: {self.BODY_FONT_PX}px;
            }}
        """)
        card_layout.addWidget(self.page_text_edit, 1)

        self.page_number_lbl = QLabel("")
        self.page_number_lbl.setAlignment(Qt.AlignCenter)
        self.page_number_lbl.setFixedHeight(self.PAGE_NUMBER_RESERVE)
        self.page_number_lbl.setStyleSheet(
            f"color: #8a8067; font-family: {FONT_SERIF}; font-size: 11px; background: transparent;"
        )
        card_layout.addWidget(self.page_number_lbl)

        page_outer_layout.addWidget(self._page_card)
        read_layout.addWidget(self._page_outer, 1)

        # ── Page nav footer ─────────────────────────────────────────────
        nav_bottom = QHBoxLayout()
        self.prev_page_btn = QPushButton("‹ Previous Page")
        self.prev_page_btn.clicked.connect(lambda: self._go_to_page(self._current_page_index - 1))
        nav_bottom.addWidget(self.prev_page_btn)
        nav_bottom.addStretch()

        self.page_counter_lbl = QLabel("Page 1 of 1")
        self.page_counter_lbl.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 12px; background: transparent;")
        nav_bottom.addWidget(self.page_counter_lbl)
        nav_bottom.addStretch()

        self.next_page_btn = QPushButton("Next Page ›")
        self.next_page_btn.clicked.connect(lambda: self._go_to_page(self._current_page_index + 1))
        nav_bottom.addWidget(self.next_page_btn)
        read_layout.addLayout(nav_bottom)

    # ── Mode switching ───────────────────────────────────────────────────

    def _set_mode(self, mode: str) -> None:
        self._mode = mode
        self.read_mode_btn.setChecked(mode == "read")
        self.edit_mode_btn.setChecked(mode == "edit")
        self.content_stack.setCurrentWidget(self.read_view if mode == "read" else self.edit_view)
        if mode == "read":
            QTimer.singleShot(0, lambda: self._refresh_read_view(preserve_fraction=False))

    # ── Book pagination / rendering ─────────────────────────────────────

    def _compute_page_card_size(self) -> tuple[int, int]:
        outer = self._page_outer
        w = outer.width() or 640
        h = outer.height() or 560
        card_w = max(360, min(700, w - 24))
        card_h = max(320, h - 8)
        return card_w, card_h

    def _refresh_read_view(self, preserve_fraction: bool = False) -> None:
        """
        Recomputes the current chapter's pagination for the pane's current
        size and re-renders the page. Called on chapter switch, mode
        switch, after a save, and (debounced) on resize. Never modifies
        chapter.content — it only re-derives the on-screen pages from it.
        """
        if not self._current_chapter:
            self._pages = []
            self._current_page_index = 0
            self.page_text_edit.clear()
            self.page_number_lbl.setText("")
            self.page_counter_lbl.setText("Page 0 of 0")
            return

        prev_total = len(self._pages) or 1
        prev_fraction = (self._current_page_index / prev_total) if preserve_fraction else 0.0

        card_w, card_h = self._compute_page_card_size()
        self._page_card.setFixedSize(card_w, card_h)
        content_w = max(card_w - 2 * self.PAGE_PADDING_H, 100)
        content_h = max(
            card_h - 2 * self.PAGE_PADDING_V - self.PAGE_NUMBER_RESERVE - self.CARD_LAYOUT_SPACING,
            100,
        )
        self.page_text_edit.setFixedSize(content_w, content_h)

        # Measure against the actual text viewport, not the theoretical
        # card geometry, for BOTH dimensions. Qt can shave a few pixels
        # off of a naive "card size minus padding" calculation for either
        # axis — not just height — even with scrollbars hidden and no
        # frame, so pagination has to read the real viewport rect (after
        # the fixed-size resize above has already taken effect) to stay
        # in exact agreement with what will actually be rendered.
        viewport_size = self.page_text_edit.viewport().size()
        measured_w = max(viewport_size.width(), 100)
        measured_h = max(viewport_size.height(), 100)

        # Fonts sized in device pixels (setPixelSize, not setPointSize) so
        # this measuring pass matches the QTextEdit's `font-size: Npx`
        # stylesheet exactly — otherwise the pagination under- or
        # over-estimates how much text actually fits per page.
        body_font = _serif_font_px(self.BODY_FONT_PX)
        title_font = _serif_font_px(self.TITLE_FONT_PX, bold=True)
        paragraphs = _split_chapter_paragraphs(self._current_chapter.content)
        self._pages = _paginate_book_pages(
            self._current_chapter.title, paragraphs, body_font, title_font, measured_w, measured_h
        )

        if preserve_fraction:
            new_index = min(int(round(prev_fraction * len(self._pages))), len(self._pages) - 1)
        else:
            new_index = 0
        # Always re-render directly (not via _go_to_page) — the page
        # *content* just changed even when the index itself didn't, so
        # the "index unchanged, skip" short-circuit in _go_to_page would
        # otherwise leave stale text on screen.
        self._current_page_index = -1
        self._go_to_page(max(new_index, 0), animate=False)
        self._update_chapter_nav_buttons()

    def _set_current_page(self, index: int) -> None:
        self._current_page_index = index
        content = self._pages[index] if self._pages else "<p>&nbsp;</p>"
        self.page_text_edit.setHtml(content)
        self.page_text_edit.verticalScrollBar().setValue(0)
        self.page_text_edit.horizontalScrollBar().setValue(0)
        total = max(len(self._pages), 1)
        self.page_number_lbl.setText(str(index + 1))
        self.page_counter_lbl.setText(f"Page {index + 1} of {total}")
        self.prev_page_btn.setEnabled(index > 0)
        self.next_page_btn.setEnabled(index < total - 1)

    def _go_to_page(self, index: int, animate: bool = True) -> None:
        if not self._pages:
            return
        index = max(0, min(index, len(self._pages) - 1))
        if index == self._current_page_index:
            return
        if animate:
            self._animate_page_change(index)
        else:
            self._set_current_page(index)

    def _animate_page_change(self, new_index: int) -> None:
        """Quick, elegant fade transition between pages — no 3D flip."""
        effect = self._page_opacity_effect
        if self._page_anim is not None:
            self._page_anim.stop()
            self._page_anim.deleteLater()
            self._page_anim = None
        anim_out = QPropertyAnimation(effect, b"opacity", self)
        anim_out.setDuration(90)
        anim_out.setStartValue(effect.opacity())
        anim_out.setEndValue(0.15)
        anim_out.setEasingCurve(QEasingCurve.OutQuad)
        anim_out.finished.connect(lambda: self._on_page_fade_out_finished(new_index))
        self._page_anim = anim_out
        anim_out.start()

    def _on_page_fade_out_finished(self, new_index: int) -> None:
        self._set_current_page(new_index)
        effect = self._page_opacity_effect
        if self._page_anim is not None:
            self._page_anim.deleteLater()
        anim_in = QPropertyAnimation(effect, b"opacity", self)
        anim_in.setDuration(130)
        anim_in.setStartValue(effect.opacity())
        anim_in.setEndValue(1.0)
        anim_in.setEasingCurve(QEasingCurve.InQuad)
        self._page_anim = anim_in
        anim_in.start()

    def _on_page_key(self, direction: str) -> None:
        if direction == "prev":
            self._go_to_page(self._current_page_index - 1)
        elif direction == "next":
            self._go_to_page(self._current_page_index + 1)
        elif direction == "first":
            self._go_to_page(0)
        elif direction == "last":
            self._go_to_page(len(self._pages) - 1 if self._pages else 0)

    # ── Chapter-to-chapter nav (within Read mode) ───────────────────────

    def _sorted_chapters(self) -> list[Chapter]:
        if not self._project:
            return []
        return sorted(self._project.chapters, key=lambda c: c.number)

    def _select_chapter_number(self, number: int) -> None:
        for i in range(self.chapter_list.count()):
            item = self.chapter_list.item(i)
            if item and item.data(Qt.UserRole) == number:
                self.chapter_list.setCurrentRow(i)
                return

    def _go_prev_chapter(self) -> None:
        chapters = self._sorted_chapters()
        if not self._current_chapter or not chapters:
            return
        idx = next((i for i, c in enumerate(chapters) if c.number == self._current_chapter.number), None)
        if idx is not None and idx > 0:
            self._select_chapter_number(chapters[idx - 1].number)

    def _go_next_chapter(self) -> None:
        chapters = self._sorted_chapters()
        if not self._current_chapter or not chapters:
            return
        idx = next((i for i, c in enumerate(chapters) if c.number == self._current_chapter.number), None)
        if idx is not None and idx < len(chapters) - 1:
            self._select_chapter_number(chapters[idx + 1].number)

    def _update_chapter_nav_buttons(self) -> None:
        chapters = self._sorted_chapters()
        if not self._current_chapter or not chapters:
            self.prev_chapter_btn.setEnabled(False)
            self.next_chapter_btn.setEnabled(False)
            self.chapter_header_lbl.setText("")
            return
        idx = next((i for i, c in enumerate(chapters) if c.number == self._current_chapter.number), None)
        self.prev_chapter_btn.setEnabled(idx is not None and idx > 0)
        self.next_chapter_btn.setEnabled(idx is not None and idx < len(chapters) - 1)
        self.chapter_header_lbl.setText(
            f"Chapter {self._current_chapter.number}\n{self._current_chapter.title}"
        )

    def resizeEvent(self, event) -> None:  # noqa: N802 — Qt override
        super().resizeEvent(event)
        if self._mode == "read" and self._current_chapter:
            self._resize_timer.start(150)

    def showEvent(self, event) -> None:  # noqa: N802 — Qt override
        super().showEvent(event)
        if self._mode == "read" and self._current_chapter:
            QTimer.singleShot(0, lambda: self._refresh_read_view(preserve_fraction=True))

    def load(self, project: Project) -> None:
        self._project = project
        self._current_chapter = None
        self._refresh_list()
        self._update_empty_state()
        # Jump straight to the first chapter instead of leaving the editor
        # blank — one less click, and the project feels alive immediately.
        if self.chapter_list.count() > 0:
            self.chapter_list.setCurrentRow(0)

    def _refresh_list(self) -> None:
        # Rebuilding the list from scratch would otherwise emit spurious
        # currentRowChanged signals (Qt auto-selects row 0 the moment the
        # first item is added back to an emptied list), which could
        # re-trigger chapter selection mid-save and clobber the editor's
        # content with the wrong chapter. Block signals across the
        # rebuild and restore the selection afterwards without notifying.
        current_num = self._current_chapter.number if self._current_chapter else None
        self.chapter_list.blockSignals(True)
        try:
            self.chapter_list.clear()
            if self._project:
                for ch in sorted(self._project.chapters, key=lambda c: c.number):
                    self.chapter_list.addItem(ChapterListItem(ch))
            if current_num is not None:
                for i in range(self.chapter_list.count()):
                    item = self.chapter_list.item(i)
                    if item and item.data(Qt.UserRole) == current_num:
                        self.chapter_list.setCurrentRow(i)
                        break
        finally:
            self.chapter_list.blockSignals(False)

    def _update_empty_state(self) -> None:
        """Show the rich empty state only when the project has zero chapters."""
        has_chapters = bool(self._project and self._project.chapters)
        self._chapters_empty_card.setVisible(not has_chapters)
        self._editor_area.setVisible(has_chapters)

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
            self._update_word_count()
            # Mode depends on the chapter's actual state, not on how it was
            # created: an empty chapter (typically just added manually, but
            # this applies equally to any chapter with no content) has
            # nothing to read yet, so it opens straight into Edit with the
            # cursor ready to type. A chapter that already has content —
            # whether written by the AI or by hand — opens in the book
            # reader as before.
            if chapter.content.strip():
                self._set_mode("read")
            else:
                self._set_mode("edit")
                self.chapter_editor.setFocus()
                cursor = self.chapter_editor.textCursor()
                cursor.movePosition(QTextCursor.MoveOperation.End)
                self.chapter_editor.setTextCursor(cursor)

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

    def _mark_chapter_ready(self) -> None:
        # A plain manual toggle — no AI call involved — so it works exactly
        # the same for a chapter written by the AI or added/edited by hand.
        if self._current_chapter:
            self._current_chapter.reviewed = True
            if self._project:
                storage.save_project(self._project)
            self._refresh_list()

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
            self._update_empty_state()
            # Open the chapter we just created. Since it starts out empty,
            # _on_chapter_selected() will land it in Edit (not the book
            # reader) with the cursor ready to type.
            self._select_chapter_number(next_num)
            self.project_changed.emit()

    def set_busy(self, busy: bool, project_name: str = "") -> None:
        tip = f"Generating content for \"{project_name}\"…" if busy and project_name else ""
        for btn in (self.write_btn, self.write_book_btn, self.change_btn):
            btn.setEnabled(not busy)
            if busy:
                btn.setToolTip(tip)
        if not busy:
            self.write_btn.setToolTip("Writes the next chapter in the story.")
            self.write_book_btn.setToolTip(
                "Writes every remaining chapter in the outline, one after another."
            )
            self.change_btn.setToolTip(
                "Tell the AI what you want to change in this chapter and it will apply your instructions."
            )

    def _update_word_count(self) -> None:
        text = self.chapter_editor.toPlainText()
        words = len(text.split()) if text.strip() else 0
        chars = len(text)

        if words == 0:
            self._wc_label.setText("0 words")
            self._read_time_label.setText("")
            return

        # Word count with thousands separator
        self._wc_label.setText(f"{words:,} words  ·  {chars:,} chars")

        # Reading time at 250 wpm
        minutes = words / 250
        if minutes < 1:
            rt = "< 1 min read"
        elif minutes < 60:
            rt = f"~{round(minutes)} min read"
        else:
            h = int(minutes // 60)
            m = int(minutes % 60)
            rt = f"~{h}h {m}m read" if m else f"~{h}h read"
        self._read_time_label.setText(rt)

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
        if not self._project.outline.strip():
            QMessageBox.information(
                self, "No Outline Yet",
                "Generate an outline first (Outline tab) — \"Generate Full Book\" writes "
                "one chapter per heading in the outline, so it needs one to follow.",
            )
            return
        outline_numbers = outline_chapter_numbers(self._project.outline)
        total = len(outline_numbers)
        written = len([
            c for c in self._project.chapters
            if c.number in outline_numbers and c.content.strip()
        ])
        remaining = max(total - written, 0)
        if remaining == 0:
            QMessageBox.information(
                self, "Book Complete",
                f"All {total} chapter(s) in the outline already have content. "
                "Add more chapters to the outline to continue, or edit chapters "
                "individually from this tab.",
            )
            return
        reply = QMessageBox.question(
            self, "Generate Full Book",
            f"This will write {remaining} remaining chapter(s) (of {total} in the "
            "outline) one after another, updating Story Memory in between. This "
            "can take a while, especially for longer books.\n\nContinue?",
            QMessageBox.Yes | QMessageBox.Cancel,
        )
        if reply != QMessageBox.Yes:
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

    def _change_chapter(self) -> None:
        if not self._current_chapter or not self._project:
            return
        if not self._current_chapter.content.strip():
            QMessageBox.information(
                self,
                "No Chapter Content",
                "This chapter has no content yet. Generate or write it first, "
                "then use \"Change Chapter\" to modify it.",
            )
            return
        dialog = ChangeChapterDialog(self._current_chapter, self)
        if dialog.exec() == QDialog.Accepted:
            instructions = dialog.instructions()
            if instructions.strip():
                self._project.current_chapter = self._current_chapter.number
                self.task_requested.emit(TaskType.CHANGE_CHAPTER, instructions)

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
            # current_chapter is a separate persisted "how far did we get"
            # pointer — it does NOT shrink automatically just because a
            # chapter was removed from the list. If we leave it pointing at
            # the old (now possibly nonexistent) chapter number, the engine's
            # _next_chapter_number() will trust that stale value over the
            # real chapter list and silently skip past the gap we just
            # created next time "Generate Chapter"/"Generate Full Book" runs — even
            # after restarting the app, since it's saved to disk below.
            self._project.current_chapter = max(
                (c.number for c in self._project.chapters), default=0
            )
            storage.save_project(self._project)
            self._current_chapter = None
            self.chapter_title_edit.clear()
            self.chapter_editor.clear()
            self._wc_label.setText("0 words")
            self._read_time_label.setText("")
            self._pages = []
            self._current_page_index = 0
            self._refresh_read_view()
            self._refresh_list()
            self._update_empty_state()
            self.project_changed.emit()

    def refresh_after_generation(self, project: Project) -> None:
        """Called when a task finishes to reload chapter content."""
        self._project = project
        current_num = self._current_chapter.number if self._current_chapter else None
        self._refresh_list()
        self._update_empty_state()
        if current_num is None and project.chapters:
            # Nothing was selected before (e.g. generating the very first
            # chapter from the empty state) — jump to the newest one so the
            # result is immediately visible instead of the editor staying blank.
            current_num = max(c.number for c in project.chapters)
        if current_num:
            chapter = next((c for c in project.chapters if c.number == current_num), None)
            if chapter:
                self._current_chapter = chapter
                self.chapter_title_edit.setText(chapter.title)
                self.chapter_editor.setPlainText(chapter.content)
                self._update_word_count()
                for i in range(self.chapter_list.count()):
                    item = self.chapter_list.item(i)
                    if item and item.data(Qt.UserRole) == current_num:
                        self.chapter_list.setCurrentRow(i)
                        break
                # A freshly generated chapter always opens in the book
                # reader — this also covers the case where the row above
                # didn't actually change (currentRowChanged wouldn't fire).
                self._set_mode("read")


class WorldTab(QWidget):
    task_requested = Signal(TaskType, str)
    content_changed = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        self._gen_header = SectionHeader("World & Setting")
        layout.addWidget(self._gen_header)

        self.editor = MarkdownEditor(
            placeholder=(
                "Describe your story world:\n\n"
                "- Geography and locations\n"
                "- Time period and technology\n"
                "- Magic or special rules\n"
                "- Political structures\n"
                "- Culture and customs\n"
                "- History relevant to the story"
            ),
        )
        self.editor.content_saved.connect(self.content_changed.emit)
        layout.addWidget(self.editor, 1)

    def load(self, project: Project) -> None:
        self.editor.set_text(project.world)

    def set_busy(self, busy: bool, project_name: str = "") -> None:
        pass  # No AI-trigger buttons remain on this tab; world updates automatically.

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
            ),
            empty_icon="🧠",
            empty_title="Story Memory fills in automatically",
            empty_description=(
                "Once you write chapters, this tracks characters, plot events, "
                "world details, and open threads for you — nothing to do here yet."
            ),
            manual_label="Add notes manually",
        )
        self.editor.content_saved.connect(self.content_changed.emit)
        layout.addWidget(self.editor, 1)

    def load(self, project: Project) -> None:
        self.editor.set_text(project.memory)

    def save_to(self, project: Project) -> None:
        project.memory = self.editor.get_text()


class AuthorProfilePanel(QWidget):
    """
    Permanent tab for editing the AuthorIntent and WritingStyle that live in
    the project. Changes are saved immediately when the user clicks Save.

    This is the persistent counterpart to the wizard fields in
    GenerateOutlineDialog: the wizard pre-fills from here and writes back
    here on accept. The author can also edit the profile directly at any
    time without going through Generate Outline.

    Labels and combo options are shown in the configured response language.
    """

    profile_changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._project: Optional[Project] = None
        self._lang: str = ""
        self._built = False  # deferred until first load() call

    def load(self, project: Project) -> None:
        self._project = project
        lang = storage.load_settings().response_language

        # Rebuild UI if language changed or first load
        if not self._built or lang != self._lang:
            self._lang = lang
            self._rebuild_ui(lang)

        # Populate fields
        intent = project.author_intent
        style = project.writing_style

        self.emotional_journey.setPlainText(intent.emotional_journey)
        self.lasting_impression.setPlainText(intent.lasting_impression)
        self.themes.setPlainText(intent.themes)
        self.unique_elements.setPlainText(intent.unique_elements)
        self.inspirations.setPlainText(intent.inspirations)
        self.avoid.setPlainText(intent.avoid)

        self.genre_tags.setText(style.genre_tags)
        _set_combo(self.narrator_pov, style.narrator_pov)
        _set_combo(self.pacing, style.pacing)
        _set_combo(self.description_density, style.description_density)
        _set_combo(self.dialogue_style, style.dialogue_style)
        _set_combo(self.violence_level, style.violence_level)
        _set_combo(self.romance_level, style.romance_level)
        _set_combo(self.target_chapter_length, style.target_chapter_length)
        self.status_lbl.setText("")

    def _rebuild_ui(self, lang: str) -> None:
        """Build (or rebuild) the full UI for the given language."""
        # Clear any existing layout
        old_layout = self.layout()
        if old_layout:
            while old_layout.count():
                item = old_layout.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
            import sip  # type: ignore
            try:
                sip.delete(old_layout)
            except Exception:
                pass

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        # Header
        hdr = QHBoxLayout()
        title_lbl = QLabel("Author Profile")
        title_lbl.setStyleSheet(f"font-size: 15px; font-weight: 700; color: {COLOR_TEXT};")
        hdr.addWidget(title_lbl)
        hdr.addStretch()
        hint = QLabel(_ui("profile_tab_hint", lang))
        hint.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 11px;")
        hdr.addWidget(hint)
        root.addLayout(hdr)

        # Scrollable form
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; }")
        body_widget = QWidget()
        body = QVBoxLayout(body_widget)
        body.setSpacing(14)
        body.setContentsMargins(0, 0, 8, 0)
        scroll.setWidget(body_widget)
        root.addWidget(scroll, 1)

        field_style = (
            f"QTextEdit {{ font-size: 12px; padding: 4px 8px; "
            f"background: {COLOR_SURFACE}; border: 1px solid {COLOR_BORDER}; "
            f"border-radius: 4px; color: {COLOR_TEXT}; }}"
            f"QTextEdit:focus {{ border-color: {COLOR_ACCENT}; }}"
        )
        line_style = (
            f"QLineEdit {{ font-size: 12px; padding: 4px 8px; "
            f"background: {COLOR_SURFACE}; border: 1px solid {COLOR_BORDER}; "
            f"border-radius: 4px; color: {COLOR_TEXT}; }}"
            f"QLineEdit:focus {{ border-color: {COLOR_ACCENT}; }}"
        )

        def _ta(placeholder: str) -> QTextEdit:
            w = QTextEdit()
            w.setPlaceholderText(placeholder)
            w.setFixedHeight(56)
            w.setStyleSheet(field_style)
            return w

        def _le(placeholder: str) -> QLineEdit:
            w = QLineEdit()
            w.setPlaceholderText(placeholder)
            w.setStyleSheet(line_style)
            return w

        # ── Intent section ────────────────────────────────────────────
        intent_box = QGroupBox(_ui("intent_group_short", lang))
        intent_form = QFormLayout(intent_box)
        intent_form.setSpacing(10)
        intent_form.setContentsMargins(14, 18, 14, 14)

        self.emotional_journey = _ta("e.g. Growing unease that never fully resolves into catharsis")
        intent_form.addRow(_ui("emotional_journey", lang), self.emotional_journey)

        self.lasting_impression = _ta("e.g. The normalcy of cruelty when systems protect it")
        intent_form.addRow(_ui("lasting_impression", lang), self.lasting_impression)

        self.themes = _ta("e.g. Institutional complicity, the cost of silence, identity under pressure")
        intent_form.addRow(_ui("themes", lang), self.themes)

        self.unique_elements = _ta("e.g. Unreliable narrator revealed gradually; non-linear structure")
        intent_form.addRow(_ui("unique_elements", lang), self.unique_elements)

        self.inspirations = _ta("e.g. Kazuo Ishiguro's restraint; Flynn's narrative misdirection")
        intent_form.addRow(_ui("inspirations", lang), self.inspirations)

        self.avoid = _ta("e.g. Redemption arcs, graphic gore, comic relief, romantic subplots")
        intent_form.addRow(_ui("avoid", lang), self.avoid)

        body.addWidget(intent_box)

        # ── Style section ─────────────────────────────────────────────
        style_box = QGroupBox(_ui("style_group_short", lang))
        style_form = QFormLayout(style_box)
        style_form.setSpacing(8)
        style_form.setContentsMargins(14, 18, 14, 14)

        self.genre_tags = _le(_ui("genre_placeholder", lang))
        self.genre_tags.setMaxLength(120)
        style_form.addRow(_ui("genre_tags", lang), self.genre_tags)

        self.narrator_pov = _make_combo(_POV_TABLE, lang)
        style_form.addRow(_ui("narrator_pov", lang), self.narrator_pov)

        self.pacing = _make_combo(_PACING_TABLE, lang)
        style_form.addRow(_ui("pacing", lang), self.pacing)

        self.description_density = _make_combo(_DENSITY_TABLE, lang)
        style_form.addRow(_ui("description_density", lang), self.description_density)

        self.dialogue_style = _make_combo(_DIALOGUE_TABLE, lang)
        style_form.addRow(_ui("dialogue_style", lang), self.dialogue_style)

        self.violence_level = _make_combo(_VIOLENCE_TABLE, lang)
        style_form.addRow(_ui("violence_level", lang), self.violence_level)

        self.romance_level = _make_combo(_ROMANCE_TABLE, lang)
        style_form.addRow(_ui("romance_level", lang), self.romance_level)

        self.target_chapter_length = _make_combo(_LENGTH_TABLE, lang)
        style_form.addRow(_ui("chapter_length", lang), self.target_chapter_length)

        body.addWidget(style_box)

        # ── Save / Load buttons ──────────────────────────────────────
        btn_row = QHBoxLayout()
        save_btn = QPushButton(_ui("save_profile_btn", lang))
        save_btn.setObjectName("accent")
        save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(save_btn)

        load_btn = QPushButton(_ui("load_profile_btn", lang))
        load_btn.clicked.connect(self._on_load_from_other_project)
        btn_row.addWidget(load_btn)
        root.addLayout(btn_row)

        self.status_lbl = QLabel("")
        self.status_lbl.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 11px;")
        self.status_lbl.setWordWrap(True)
        root.addWidget(self.status_lbl)

        self._built = True

    # ── Save ──────────────────────────────────────────────────────────

    def _on_save(self) -> None:
        if not self._project:
            return
        self._project.author_intent = AuthorIntent(
            emotional_journey=self.emotional_journey.toPlainText().strip(),
            lasting_impression=self.lasting_impression.toPlainText().strip(),
            themes=self.themes.toPlainText().strip(),
            unique_elements=self.unique_elements.toPlainText().strip(),
            inspirations=self.inspirations.toPlainText().strip(),
            avoid=self.avoid.toPlainText().strip(),
        )
        self._project.writing_style = WritingStyle(
            genre_tags=self.genre_tags.text().strip(),
            narrator_pov=_combo_value(self.narrator_pov),
            pacing=_combo_value(self.pacing),
            description_density=_combo_value(self.description_density),
            dialogue_style=_combo_value(self.dialogue_style),
            violence_level=_combo_value(self.violence_level),
            romance_level=_combo_value(self.romance_level),
            target_chapter_length=_combo_value(self.target_chapter_length),
        )
        self.profile_changed.emit()
        self.status_lbl.setText("")

    # ── Load from another project ───────────────────────────────────────

    def _on_load_from_other_project(self) -> None:
        """
        Copy Creative Intent + Writing Style from another project into
        this form, so the user doesn't have to retype a profile they've
        already written once. This only fills the fields shown on
        screen — nothing is written to disk until the user clicks
        "Save Profile", so they get a chance to review first.
        """
        if not self._project:
            return
        lang = self._lang

        candidates = [
            p for p in storage.load_all_projects()
            if p.id != self._project.id
            and not (p.author_intent.is_empty() and p.writing_style.is_empty())
        ]
        if not candidates:
            QMessageBox.information(
                self, _ui("load_profile_dialog_title", lang),
                _ui("load_profile_no_other_projects", lang),
            )
            return

        dialog = QDialog(self)
        dialog.setWindowTitle(_ui("load_profile_dialog_title", lang))
        dialog.resize(420, 380)
        layout = QVBoxLayout(dialog)

        hint = QLabel(_ui("load_profile_dialog_hint", lang))
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 11px;")
        layout.addWidget(hint)

        list_widget = QListWidget()
        for p in candidates:
            item = QListWidgetItem(p.title)
            item.setData(Qt.UserRole, p.id)
            list_widget.addItem(item)
        list_widget.setCurrentRow(0)
        layout.addWidget(list_widget, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        # Double-clicking a project loads it immediately, same as OK.
        list_widget.itemDoubleClicked.connect(dialog.accept)

        if dialog.exec() != QDialog.Accepted:
            return
        selected = list_widget.currentItem()
        if not selected:
            return
        source = next((p for p in candidates if p.id == selected.data(Qt.UserRole)), None)
        if not source:
            return

        intent = source.author_intent
        style = source.writing_style

        self.emotional_journey.setPlainText(intent.emotional_journey)
        self.lasting_impression.setPlainText(intent.lasting_impression)
        self.themes.setPlainText(intent.themes)
        self.unique_elements.setPlainText(intent.unique_elements)
        self.inspirations.setPlainText(intent.inspirations)
        self.avoid.setPlainText(intent.avoid)

        self.genre_tags.setText(style.genre_tags)
        _set_combo(self.narrator_pov, style.narrator_pov)
        _set_combo(self.pacing, style.pacing)
        _set_combo(self.description_density, style.description_density)
        _set_combo(self.dialogue_style, style.dialogue_style)
        _set_combo(self.violence_level, style.violence_level)
        _set_combo(self.romance_level, style.romance_level)
        _set_combo(self.target_chapter_length, style.target_chapter_length)

        self.status_lbl.setText(
            _ui("load_profile_loaded_status", lang).format(title=source.title)
        )


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
        self.outline_tab.profile_updated.connect(self._on_profile_updated)
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

        self.author_profile_tab = AuthorProfilePanel()
        self.author_profile_tab.profile_changed.connect(self._on_profile_updated)
        self.tabs.addTab(self.author_profile_tab, "Author")

        self.stats_tab = StatsTab()
        self.tabs.addTab(self.stats_tab, "📊 Stats")

        self.search_tab = SearchTab()
        self.tabs.addTab(self.search_tab, "🔍 Search")

        layout.addWidget(self.tabs)

    def load_project(self, project: Project) -> None:
        self._project = project
        self.synopsis_tab.load(project)
        self.outline_tab.load(project)
        self.chars_tab.load(project)
        self.world_tab.load(project)
        self.chapters_tab.load(project)
        self.memory_tab.load(project)
        self.author_profile_tab.load(project)
        self.stats_tab.load(project)
        self.search_tab.load(project)

    def refresh_after_task(self, project: Project) -> None:
        """Called after an AI task finishes to refresh content."""
        self._project = project
        self.synopsis_tab.load(project)
        self.outline_tab.load(project)
        self.chars_tab.load(project)
        self.world_tab.load(project)
        self.memory_tab.load(project)
        self.chapters_tab.refresh_after_generation(project)
        self.author_profile_tab.load(project)
        self.stats_tab.load(project)
        self.search_tab.load(project)

    def set_busy(self, busy: bool, project_name: str = "") -> None:
        """Disable/enable all AI-trigger buttons across every story sub-tab."""
        self.synopsis_tab.set_busy(busy, project_name)
        self.outline_tab.set_busy(busy, project_name)
        self.world_tab.set_busy(busy, project_name)
        self.chapters_tab.set_busy(busy, project_name)

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

    def _on_profile_updated(self) -> None:
        """AuthorIntent/WritingStyle changed — persist immediately."""
        self._save_project()
