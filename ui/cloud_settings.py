"""
Settings editor for cloud API providers.

One card per provider preset. Each card holds the API key, an optional base
URL (for proxies or self-hosted servers), and the model names to offer in the
Text Models tab. "Fetch model list" asks the provider which models the key can
use, in a background thread so the window stays responsive.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QThread
from PySide6.QtWidgets import (
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from engine import cloud_client
from engine.cloud_providers import PRESETS, ProviderPreset, provider_config
from ui.styles import COLOR_TEXT_DIM
from ui.workers import CallWorker, start_worker


class _ProviderCard(QGroupBox):
    def __init__(self, preset: ProviderPreset, parent: Optional[QWidget] = None) -> None:
        super().__init__(preset.label, parent)
        self.preset = preset
        self._thread: Optional[QThread] = None
        self._worker: Optional[CallWorker] = None

        form = QFormLayout(self)
        form.setSpacing(8)
        form.setContentsMargins(14, 16, 14, 12)

        self.key_input = QLineEdit()
        self.key_input.setEchoMode(QLineEdit.Password)
        self.key_input.setPlaceholderText(
            "Paste your API key" if preset.needs_api_key else "Not required for this server"
        )
        form.addRow("API key", self.key_input)

        self.base_input = QLineEdit()
        self.base_input.setPlaceholderText(preset.default_base_url or "https://your-server/v1")
        self.base_input.setToolTip(
            "Leave empty to use the provider's standard address."
        )
        form.addRow("Base URL", self.base_input)

        self.models_input = QLineEdit()
        self.models_input.setPlaceholderText("model-a, model-b")
        self.models_input.setToolTip(
            "Model names to offer in Text Models, separated by commas. "
            "Use “Fetch model list” to fill this in from the provider."
        )
        form.addRow("Models", self.models_input)

        row = QHBoxLayout()
        self.fetch_btn = QPushButton("Fetch model list")
        self.fetch_btn.setObjectName("subtle")
        self.fetch_btn.clicked.connect(self._fetch)
        row.addWidget(self.fetch_btn)
        row.addStretch()
        form.addRow("", row)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 12px;")
        form.addRow("", self.status_label)

    def load(self, config: dict) -> None:
        self.key_input.setText(config["api_key"])
        self.base_input.setText(config["base_url"])
        self.models_input.setText(", ".join(config["models"]))

    def collect(self) -> dict:
        models = [m.strip() for m in self.models_input.text().split(",") if m.strip()]
        return {
            "api_key": self.key_input.text().strip(),
            "base_url": self.base_input.text().strip(),
            "models": models,
        }

    def _set_status(self, ok: Optional[bool], text: str) -> None:
        color = "#3d9970" if ok else ("#c0392b" if ok is False else COLOR_TEXT_DIM)
        self.status_label.setStyleSheet(f"color: {color}; font-size: 12px;")
        self.status_label.setText(text)

    def _fetch(self) -> None:
        if self._thread and self._thread.isRunning():
            return
        config = self.collect()
        provider_id = self.preset.id

        def job() -> list[str]:
            conn = cloud_client.connection_for({provider_id: config}, provider_id)
            return cloud_client.list_remote_models(conn)

        self.fetch_btn.setEnabled(False)
        self._set_status(None, "Fetching model list…")
        self._worker = CallWorker(job)
        self._thread = start_worker(self, self._worker, self._on_fetched)

    def _on_fetched(self, outcome) -> None:
        ok, value = outcome
        self.fetch_btn.setEnabled(True)
        if not ok:
            self._set_status(False, f"✗ {value}")
        elif not value:
            self._set_status(False, "✗ Connected, but the provider returned no models.")
        else:
            self.models_input.setText(", ".join(value))
            self._set_status(
                True,
                f"✓ {len(value)} model{'s' if len(value) != 1 else ''} found. "
                "Keep only the ones you want to use.",
            )
        self._worker = None
        self._thread = None


class CloudProvidersBox(QWidget):
    """All provider cards, in a column."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self._cards: dict[str, _ProviderCard] = {}
        for preset in PRESETS:
            card = _ProviderCard(preset, self)
            self._cards[preset.id] = card
            layout.addWidget(card)

    def load(self, cloud_providers: dict) -> None:
        for provider_id, card in self._cards.items():
            card.load(provider_config(cloud_providers, provider_id))

    def collect(self) -> dict:
        return {provider_id: card.collect() for provider_id, card in self._cards.items()}
