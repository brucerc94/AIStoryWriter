"""
Settings Panel.

Two sections:
1. App Settings — local GGUF folder (llama.cpp), Ollama server, context size,
   generation options, and llama.cpp-only performance options
2. Model Assignments — pick a model for each task type. Two kinds of models
   can be picked and each is labelled with its backend:
     [llama.cpp] a .gguf file run locally by llama-cpp-python
     [Ollama]    a model installed on an Ollama server
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from engine import ollama_client, storage
from engine.image_engine import get_image_model_profile
from engine.models import AppSettings, ImageBackend, Project, TaskType
from ui.styles import (
    COLOR_ACCENT,
    COLOR_BORDER,
    COLOR_SURFACE,
    COLOR_SURFACE_RAISED,
    COLOR_TEXT,
    COLOR_TEXT_DIM,
    COLOR_TEXT_MUTED,
)


TASK_LABELS: dict[TaskType, str] = {
    TaskType.CHAT: "Chat / Q&A",
    TaskType.WRITE_SYNOPSIS: "Write Synopsis",
    TaskType.GENERATE_OUTLINE: "Generate Outline",
    TaskType.REVIEW_OUTLINE: "Review Outline",
    TaskType.GENERATE_WORLD: "Generate World",
    TaskType.WRITE_CHAPTER: "Write Chapter",
    TaskType.WRITE_BOOK: "Write Book",
    TaskType.REVIEW_CHAPTER: "Review Chapter",
    TaskType.REWRITE_CHAPTER: "Rewrite Chapter",
    TaskType.UPDATE_MEMORY: "Update Story Memory",
    TaskType.CONVERSATION_SUMMARY: "Conversation Summary",
}


NOT_ASSIGNED_LABEL = "— not assigned —"


def _model_label(ref: str, listed: bool = True) -> str:
    """Label shown in every model combo: always says which backend runs it."""
    label = ollama_client.display_name(ref)
    return label if listed else f"{label}  — not found"


class ModelPicker(QWidget):
    """A row with a task label, a model combo (llama.cpp GGUF files and
    Ollama models), and that task's own generation temperature — different
    models often want very different temperatures (fast/precise for
    summaries vs. large/creative for chapters), so it lives right next to
    the model it applies to."""

    model_changed = Signal(TaskType, str)
    temperature_changed = Signal(TaskType, float)
    top_p_changed = Signal(TaskType, float)
    top_k_changed = Signal(TaskType, int)

    def __init__(self, task: TaskType, models: list[str], parent=None) -> None:
        super().__init__(parent)
        self.task = task
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        lbl = QLabel(TASK_LABELS.get(task, task.value))
        lbl.setFixedWidth(160)
        lbl.setStyleSheet(f"color: {COLOR_TEXT}; font-size: 13px;")
        layout.addWidget(lbl)

        self.combo = QComboBox()
        self.combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.combo.setToolTip(
            "[llama.cpp] = GGUF file from your models folder, run locally.\n"
            "[Ollama] = model installed on your Ollama server."
        )
        self.combo.addItem(NOT_ASSIGNED_LABEL, "")
        for m in models:
            self.combo.addItem(_model_label(m), m)
        self.combo.currentIndexChanged.connect(self._on_changed)
        layout.addWidget(self.combo, 1)

        browse_btn = QPushButton("Browse…")
        browse_btn.setFixedWidth(70)
        browse_btn.clicked.connect(self._browse)
        layout.addWidget(browse_btn)

        temp_lbl = QLabel("Temp")
        temp_lbl.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 11px;")
        layout.addWidget(temp_lbl)

        self.temp_spin = QDoubleSpinBox()
        self.temp_spin.setRange(0.0, 2.0)
        self.temp_spin.setSingleStep(0.05)
        self.temp_spin.setDecimals(2)
        self.temp_spin.setValue(0.7)
        self.temp_spin.setFixedWidth(70)
        self.temp_spin.setToolTip(
            f"Generation temperature for {TASK_LABELS.get(task, task.value)}. "
            "0.0 = deterministic/focused, 2.0 = max randomness."
        )
        self.temp_spin.valueChanged.connect(self._on_temperature_changed)
        layout.addWidget(self.temp_spin)

        top_p_lbl = QLabel("Top P")
        top_p_lbl.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 11px;")
        layout.addWidget(top_p_lbl)

        self.top_p_spin = QDoubleSpinBox()
        self.top_p_spin.setRange(0.0, 1.0)
        self.top_p_spin.setSingleStep(0.05)
        self.top_p_spin.setDecimals(2)
        self.top_p_spin.setValue(0.9)
        self.top_p_spin.setFixedWidth(70)
        self.top_p_spin.setToolTip(
            f"Nucleus sampling (Top P) for {TASK_LABELS.get(task, task.value)}. "
            "Only tokens within this cumulative probability mass are considered. "
            "1.0 = no filtering."
        )
        self.top_p_spin.valueChanged.connect(self._on_top_p_changed)
        layout.addWidget(self.top_p_spin)

        top_k_lbl = QLabel("Top K")
        top_k_lbl.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 11px;")
        layout.addWidget(top_k_lbl)

        self.top_k_spin = QSpinBox()
        self.top_k_spin.setRange(0, 1000)
        self.top_k_spin.setSingleStep(1)
        self.top_k_spin.setValue(40)
        self.top_k_spin.setFixedWidth(70)
        self.top_k_spin.setToolTip(
            f"Top K for {TASK_LABELS.get(task, task.value)}. Only the K most "
            "likely next tokens are considered. 0 = disabled/no limit."
        )
        self.top_k_spin.valueChanged.connect(self._on_top_k_changed)
        layout.addWidget(self.top_k_spin)

    def set_value(self, path: str) -> None:
        """Show a stored assignment. Programmatic: does not emit model_changed."""
        self.combo.blockSignals(True)
        try:
            if not path:
                # Explicitly select "not assigned". Matching on itemData("")
                # is unreliable (Qt may return None for empty data), which
                # used to leave the previous project's model on screen.
                self.combo.setCurrentIndex(0)
                return

            for i in range(1, self.combo.count()):
                if self.combo.itemData(i) == path:
                    self.combo.setCurrentIndex(i)
                    return

            # Assigned, but not in the current lists (GGUF folder changed,
            # file moved, Ollama offline). Show it honestly as missing.
            self.combo.addItem(_model_label(path, listed=False), path)
            self.combo.setCurrentIndex(self.combo.count() - 1)
        finally:
            self.combo.blockSignals(False)

    def get_value(self) -> str:
        return self.combo.currentData() or ""

    def set_temperature(self, temperature: float) -> None:
        self.temp_spin.blockSignals(True)
        self.temp_spin.setValue(temperature)
        self.temp_spin.blockSignals(False)

    def get_temperature(self) -> float:
        return self.temp_spin.value()

    def _on_temperature_changed(self, value: float) -> None:
        self.temperature_changed.emit(self.task, value)

    def set_top_p(self, top_p: float) -> None:
        self.top_p_spin.blockSignals(True)
        self.top_p_spin.setValue(top_p)
        self.top_p_spin.blockSignals(False)

    def get_top_p(self) -> float:
        return self.top_p_spin.value()

    def _on_top_p_changed(self, value: float) -> None:
        self.top_p_changed.emit(self.task, value)

    def set_top_k(self, top_k: int) -> None:
        self.top_k_spin.blockSignals(True)
        self.top_k_spin.setValue(top_k)
        self.top_k_spin.blockSignals(False)

    def get_top_k(self) -> int:
        return self.top_k_spin.value()

    def _on_top_k_changed(self, value: int) -> None:
        self.top_k_changed.emit(self.task, value)

    def update_models(self, models: list[str]) -> None:
        current = self.get_value()
        self.combo.blockSignals(True)
        self.combo.clear()
        self.combo.addItem(NOT_ASSIGNED_LABEL, "")
        for m in models:
            self.combo.addItem(_model_label(m), m)
        self.combo.blockSignals(False)
        if current:
            self.set_value(current)

    def _on_changed(self) -> None:
        self.model_changed.emit(self.task, self.get_value())

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select GGUF Model for llama.cpp",
            "",
            "GGUF Models (*.gguf);;All Files (*)",
        )
        if path:
            self.set_value(path)
            # Picking a file is a user action, so it must save like any other change.
            self._on_changed()


class AppSettingsWidget(QWidget):
    settings_changed = Signal(AppSettings)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._settings: Optional[AppSettings] = None
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)




        def _lbl(text: str) -> QLabel:
            lbl = QLabel(text)
            lbl.setMinimumWidth(160)
            lbl.setWordWrap(False)
            return lbl

        box = QGroupBox("Application")
        form = QFormLayout(box)
        form.setSpacing(12)
        form.setContentsMargins(14, 18, 14, 18)
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)


        dir_row = QHBoxLayout()
        self.models_dir_input = QLineEdit()
        self.models_dir_input.setPlaceholderText("Path to folder containing .gguf files")
        dir_row.addWidget(self.models_dir_input, 1)
        browse_dir_btn = QPushButton("Browse…")
        browse_dir_btn.setFixedWidth(70)
        browse_dir_btn.clicked.connect(self._browse_models_dir)
        dir_row.addWidget(browse_dir_btn)
        form.addRow("GGUF Folder (llama.cpp)", dir_row)


        self.ctx_spin = QSpinBox()
        self.ctx_spin.setRange(512, 131072)
        self.ctx_spin.setSingleStep(512)
        self.ctx_spin.setValue(4096)
        self.ctx_spin.setSuffix(" tokens")
        self.ctx_spin.setMinimumWidth(160)
        self.ctx_spin.setToolTip(
            "The model's total window: prompt + reply combined. This is a "
            "hard ceiling set by the model itself — it does not control how "
            "much the app asks the model to write in one go (see \"Max "
            "Tokens per Pass\" below).\n\n"
            "Applies to both llama.cpp and Ollama models."
        )
        form.addRow("Context Size", self.ctx_spin)


        self.gpu_spin = QSpinBox()
        self.gpu_spin.setRange(0, 999)
        self.gpu_spin.setValue(0)
        self.gpu_spin.setMinimumWidth(160)
        self.gpu_spin.setToolTip(
            "Number of layers to offload to GPU. 0 = CPU only.\n\n"
            "llama.cpp models only. Ollama decides GPU usage on its own."
        )
        form.addRow("GPU Layers (llama.cpp)", self.gpu_spin)


        self.threads_spin = QSpinBox()
        self.threads_spin.setRange(1, 64)
        self.threads_spin.setValue(4)
        self.threads_spin.setMinimumWidth(160)
        self.threads_spin.setToolTip(
            "Threads used for single-token generation (llama.cpp's n_threads).\n\n"
            "llama.cpp models only."
        )
        form.addRow("CPU Threads (llama.cpp)", self.threads_spin)


        self.threads_batch_spin = QSpinBox()
        self.threads_batch_spin.setRange(0, 64)
        self.threads_batch_spin.setValue(0)
        self.threads_batch_spin.setMinimumWidth(200)
        self.threads_batch_spin.setSpecialValueText("Auto (same as CPU Threads)")
        self.threads_batch_spin.setToolTip(
            "Threads used for prompt/batch processing (llama.cpp's "
            "n_threads_batch) — separate from CPU Threads above, which only "
            "covers single-token generation. Prompt processing is highly "
            "parallel, so this often benefits from a higher value than CPU "
            "Threads, especially on machines with many cores.\n\n"
            "0 = auto: not passed explicitly, so llama-cpp-python falls "
            "back to its own default (mirrors CPU Threads).\n\n"
            "Only applied if the installed llama-cpp-python build actually "
            "exposes this parameter. llama.cpp models only."
        )
        form.addRow("CPU Threads (Batch, llama.cpp)", self.threads_batch_spin)


        self.autosave_check = QCheckBox("Auto-save after AI responses")
        self.autosave_check.setChecked(True)
        form.addRow("", self.autosave_check)


        self.enable_thinking_check = QCheckBox("Enable Thinking")
        self.enable_thinking_check.setToolTip(
            "When enabled, Qwen models that support it are asked to think "
            "before answering. Applies to llama.cpp and Ollama Qwen models."
        )
        form.addRow("", self.enable_thinking_check)


        self.allow_nsfw_check = QCheckBox("Allow mature / unrestricted content (NSFW)")
        self.allow_nsfw_check.setToolTip(
            "When enabled, the model receives an explicit instruction that it may write "
            "adult content without restrictions — graphic violence, explicit sexuality, "
            "strong language, and dark themes — for the creative writing tasks: synopsis, "
            "outline, chapters, rewrite, world-building, and story memory.\n\n"
            "Has no effect on review, chat, or summary tasks.\n\n"
            "Use this when your model has been uncensored/fine-tuned for adult fiction "
            "and you want it to follow your story without softening scenes."
        )
        form.addRow("", self.allow_nsfw_check)


        self.log_full_prompts_check = QCheckBox("Show full prompt sent to the model in the console/log")
        self.log_full_prompts_check.setToolTip(
            "When enabled, every call to the model logs the complete system+user "
            "prompt text at INFO level, not just the token count. Off by default — "
            "prompts can be long; turn this on only when you need to see exactly "
            "what's being sent, e.g. while debugging."
        )
        form.addRow("", self.log_full_prompts_check)

        layout.addWidget(box)


        ollama_box = QGroupBox("Ollama")
        ollama_form = QFormLayout(ollama_box)
        ollama_form.setSpacing(12)
        ollama_form.setContentsMargins(14, 18, 14, 18)
        ollama_form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        ollama_form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)

        ollama_note = QLabel(
            "Models marked [Ollama] are sent to this Ollama server. Install a "
            "model with `ollama pull <name>` first. Ollama manages its own "
            "memory and GPU use, so the llama.cpp options below don't apply to it."
        )
        ollama_note.setWordWrap(True)
        ollama_note.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 11px;")
        ollama_form.addRow(ollama_note)

        ollama_url_row = QHBoxLayout()
        self.ollama_url_input = QLineEdit()
        self.ollama_url_input.setPlaceholderText(ollama_client.DEFAULT_OLLAMA_URL)
        self.ollama_url_input.setToolTip(
            "Address of the Ollama server. The default is the local Ollama "
            "service. Change it only if Ollama runs on another machine."
        )
        ollama_url_row.addWidget(self.ollama_url_input, 1)
        check_ollama_btn = QPushButton("Check Connection")
        check_ollama_btn.setFixedWidth(130)
        check_ollama_btn.clicked.connect(self._check_ollama)
        ollama_url_row.addWidget(check_ollama_btn)
        ollama_form.addRow("Server URL", ollama_url_row)

        self.ollama_status_label = QLabel("Not checked yet.")
        self.ollama_status_label.setWordWrap(True)
        self.ollama_status_label.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 12px;")
        ollama_form.addRow("", self.ollama_status_label)

        layout.addWidget(ollama_box)


        gen_box = QGroupBox("Generation")
        gen_form = QFormLayout(gen_box)
        gen_form.setSpacing(12)
        gen_form.setContentsMargins(14, 18, 14, 18)
        gen_form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        gen_form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)










        self.language_input = QLineEdit()
        self.language_input.setPlaceholderText("e.g. Español, English, Français…")
        self.language_input.setToolTip(
            "The model is told explicitly to respond in this language, "
            "since the app's own built-in instructions are all in English. "
            "Leave blank to not add this instruction."
        )
        gen_form.addRow("Response Language", self.language_input)


        self.max_tokens_spin = QSpinBox()
        self.max_tokens_spin.setRange(256, 32000)
        self.max_tokens_spin.setSingleStep(256)
        self.max_tokens_spin.setValue(4000)
        self.max_tokens_spin.setSuffix(" tokens")
        self.max_tokens_spin.setToolTip(
            "Maximum reply length for generation tasks that use an explicit "
            "per-pass limit. Write Chapter and Write Book automatically use "
            "the available Context Size after the prompt instead of being "
            "artificially capped by this value. If the model stops before "
            "finishing a chapter, the app can continue in another pass.\n\n"
            "This is separate from Context Size: Context Size is the model's "
            "total window (prompt + reply)."
        )
        gen_form.addRow("Max Tokens per Pass", self.max_tokens_spin)


        self.system_prompt_input = QPlainTextEdit()
        self.system_prompt_input.setPlaceholderText(
            "Optional instructions appended to every system prompt, for every task "
            "(chat, writing, review, memory, summaries).\n\n"
            "Examples:\n"
            "- Tone/POV/style rules the model should always follow\n"
            "- Content rating notes\n"
            "- House style guide reminders"
        )
        self.system_prompt_input.setFixedHeight(140)
        gen_form.addRow("Custom System Prompt", self.system_prompt_input)

        layout.addWidget(gen_box)


        moe_box = QGroupBox("MoE Performance (llama.cpp only)")
        moe_form = QFormLayout(moe_box)
        moe_form.setSpacing(12)
        moe_form.setContentsMargins(14, 18, 14, 18)
        moe_form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        moe_form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)

        moe_note = QLabel(
            "For llama.cpp models only; Ollama models ignore these. Only "
            "applied when a Mixture-of-Experts GGUF is detected (Qwen MoE, "
            "Mixtral, GPT-OSS-style checkpoints, …) — dense models are "
            "completely unaffected. Also only used if the installed "
            "llama-cpp-python build actually supports these parameters."
        )
        moe_note.setWordWrap(True)
        moe_note.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 11px;")
        moe_form.addRow(moe_note)

        self.moe_batch_spin = QSpinBox()
        self.moe_batch_spin.setRange(128, 8192)
        self.moe_batch_spin.setSingleStep(128)
        self.moe_batch_spin.setValue(1024)
        self.moe_batch_spin.setToolTip(
            "Batch size (llama.cpp's n_batch) used for MoE models. Larger "
            "batches amortize per-token expert-routing overhead better than "
            "dense models need, so MoE models often benefit from a bigger "
            "value than you'd otherwise use."
        )
        moe_form.addRow("MoE Batch Size", self.moe_batch_spin)

        self.moe_ubatch_spin = QSpinBox()
        self.moe_ubatch_spin.setRange(128, 8192)
        self.moe_ubatch_spin.setSingleStep(128)
        self.moe_ubatch_spin.setValue(1024)
        self.moe_ubatch_spin.setToolTip(
            "Micro-batch size (llama.cpp's n_ubatch) used for MoE models. "
            "Usually kept equal to MoE Batch Size."
        )
        moe_form.addRow("MoE Micro-Batch Size", self.moe_ubatch_spin)

        layout.addWidget(moe_box)










        img_box = QGroupBox("Image Generation")
        img_form = QFormLayout(img_box)
        img_form.setSpacing(12)
        img_form.setContentsMargins(14, 18, 14, 18)
        img_form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        img_form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)

        img_note = QLabel(
            "Configure the local image generation backend. "
            "For monolithic checkpoints (SD 1.x, SDXL…) only set "
            "\"Diffusion Model\". For multi-component architectures like "
            "Z-Image-Turbo and Qwen Image 2.1, also set \"Text Encoder\" and \"VAE\"."
        )
        img_note.setWordWrap(True)
        img_note.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 11px;")
        img_form.addRow(img_note)


        img_model_row = QHBoxLayout()
        self.image_model_input = QLineEdit()
        self.image_model_input.setPlaceholderText(
            "Path to diffusion model checkpoint (.gguf or .safetensors)…"
        )
        self.image_model_input.setToolTip(
            "Primary image model file.\n\n"
            "• Monolithic checkpoints (SD 1.x, SDXL, …): the full checkpoint file.\n"
            "• Multi-component (Z-Image-Turbo, Qwen Image 2.1, Flux, Anima, …): the standalone "
            "  diffusion model GGUF — e.g. z_image_turbo-Q4_0.gguf.\n\n"
            "Leave \"Text Encoder\" and \"VAE\" empty for monolithic checkpoints."
        )
        img_model_row.addWidget(self.image_model_input, 1)

        browse_img_btn = QPushButton("Browse…")
        browse_img_btn.setFixedWidth(70)
        browse_img_btn.clicked.connect(self._browse_image_model)
        img_model_row.addWidget(browse_img_btn)
        img_form.addRow("Diffusion Model", img_model_row)

        self.image_model_detected_label = QLabel("Detected model: —")
        self.image_model_detected_label.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font-size: 11px;"
        )
        img_form.addRow("", self.image_model_detected_label)


        img_enc_row = QHBoxLayout()
        self.image_text_encoder_input = QLineEdit()
        self.image_text_encoder_input.setPlaceholderText(
            "Path to standalone text encoder / LLM (.gguf or .safetensors)…"
        )
        self.image_text_encoder_input.setToolTip(
            "Standalone text encoder / LLM for multi-component architectures.\n\n"
            "• Z-Image-Turbo: Qwen3-4B-ZImage-Heretic-Genesis-Q8.gguf\n"
            "• Qwen Image 2.1: Qwen3-VL-8B-Instruct GGUF or INT8 convrot safetensors\n"
            "• Flux 2: Mistral-Small-3.2-…Q4_K_M.gguf\n"
            "• Anima / Klein: Qwen3-4B-Instruct-2507-Q4_K_M.gguf\n\n"
            "Leave empty for monolithic checkpoints (SD 1.x, SDXL, …)."
        )
        img_enc_row.addWidget(self.image_text_encoder_input, 1)

        browse_enc_btn = QPushButton("Browse…")
        browse_enc_btn.setFixedWidth(70)
        browse_enc_btn.clicked.connect(self._browse_image_text_encoder)
        img_enc_row.addWidget(browse_enc_btn)
        img_form.addRow("Text Encoder", img_enc_row)


        img_vae_row = QHBoxLayout()
        self.image_vae_input = QLineEdit()
        self.image_vae_input.setPlaceholderText(
            "Optional: path to standalone VAE (.safetensors or .gguf)…"
        )
        self.image_vae_input.setToolTip(
            "Standalone VAE for multi-component architectures.\n\n"
            "• Z-Image-Turbo: ae.safetensors\n"
            "• Qwen Image 2.1: qwen_image_2.1_vae_bf16.safetensors\n"
            "• Flux (schnell / dev): ae.safetensors or ae-f16.gguf\n\n"
            "Leave empty when the VAE is baked into the main checkpoint."
        )
        img_vae_row.addWidget(self.image_vae_input, 1)

        browse_vae_btn = QPushButton("Browse…")
        browse_vae_btn.setFixedWidth(70)
        browse_vae_btn.clicked.connect(self._browse_image_vae)
        img_vae_row.addWidget(browse_vae_btn)
        img_form.addRow("VAE", img_vae_row)


        self.image_backend_combo = QComboBox()
        self.image_backend_combo.addItem(
            "stable-diffusion.cpp", ImageBackend.STABLE_DIFFUSION_CPP.value
        )
        self.image_backend_combo.setToolTip(
            "Which local image generation backend to use.\n"
            "Only stable-diffusion.cpp is available now; more will be added later."
        )
        img_form.addRow("Image Backend", self.image_backend_combo)


        img_out_row = QHBoxLayout()
        self.image_output_dir_input = QLineEdit()
        self.image_output_dir_input.setPlaceholderText(
            "Where to save generated images (leave blank for default)…"
        )
        img_out_row.addWidget(self.image_output_dir_input, 1)

        browse_out_btn = QPushButton("Browse…")
        browse_out_btn.setFixedWidth(70)
        browse_out_btn.clicked.connect(self._browse_image_output_dir)
        img_out_row.addWidget(browse_out_btn)
        img_form.addRow("Output Directory", img_out_row)


        dims_row = QHBoxLayout()
        dims_row.setSpacing(12)

        img_w_lbl = QLabel("Default Width")
        img_w_lbl.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 12px;")
        dims_row.addWidget(img_w_lbl)

        self.image_width_spin = QSpinBox()
        self.image_width_spin.setRange(64, 2048)
        self.image_width_spin.setSingleStep(64)
        self.image_width_spin.setValue(512)
        self.image_width_spin.setSuffix(" px")
        self.image_width_spin.setFixedWidth(100)
        dims_row.addWidget(self.image_width_spin)

        img_h_lbl = QLabel("Default Height")
        img_h_lbl.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 12px;")
        dims_row.addWidget(img_h_lbl)

        self.image_height_spin = QSpinBox()
        self.image_height_spin.setRange(64, 2048)
        self.image_height_spin.setSingleStep(64)
        self.image_height_spin.setValue(512)
        self.image_height_spin.setSuffix(" px")
        self.image_height_spin.setFixedWidth(100)
        dims_row.addWidget(self.image_height_spin)

        dims_row.addStretch()
        img_form.addRow("", dims_row)


        gen_row = QHBoxLayout()
        gen_row.setSpacing(12)

        steps_lbl = QLabel("Default Steps")
        steps_lbl.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 12px;")
        gen_row.addWidget(steps_lbl)

        self.image_steps_spin = QSpinBox()
        self.image_steps_spin.setRange(1, 200)
        self.image_steps_spin.setValue(20)
        self.image_steps_spin.setFixedWidth(80)
        self.image_steps_spin.setToolTip(
            "Number of diffusion sampling steps.\n"
            "Recommended defaults are detected from the selected model family."
        )
        gen_row.addWidget(self.image_steps_spin)

        cfg_lbl = QLabel("CFG Scale")
        cfg_lbl.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 12px;")
        gen_row.addWidget(cfg_lbl)

        self.image_cfg_spin = QDoubleSpinBox()
        self.image_cfg_spin.setRange(1.0, 30.0)
        self.image_cfg_spin.setSingleStep(0.5)
        self.image_cfg_spin.setValue(7.0)
        self.image_cfg_spin.setDecimals(1)
        self.image_cfg_spin.setFixedWidth(80)
        self.image_cfg_spin.setToolTip(
            "Classifier-free guidance scale.\n"
            "Recommended defaults are detected from the selected model family."
        )
        gen_row.addWidget(self.image_cfg_spin)

        gen_row.addStretch()
        img_form.addRow("", gen_row)


        lora_note = QLabel(
            "LoRA adapters are applied at generation time. "
            "Each LoRA must be a .safetensors file compatible with the selected diffusion model. "
            "Weight controls adapter strength (typical range 0.5 – 1.0, negative values invert). "
            "Use the Enable checkbox to toggle a LoRA without removing it."
        )
        lora_note.setWordWrap(True)
        lora_note.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 11px;")
        img_form.addRow(lora_note)


        self.lora_table = QTableWidget(0, 5)
        self.lora_table.setHorizontalHeaderLabels(["On", "LoRA Path", "Weight", "Trigger", ""])
        self.lora_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.lora_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.lora_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.lora_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.lora_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.lora_table.verticalHeader().setVisible(False)
        self.lora_table.setSelectionMode(QAbstractItemView.NoSelection)
        self.lora_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.lora_table.setMinimumHeight(100)
        self.lora_table.setMaximumHeight(220)
        img_form.addRow("LoRA Adapters", self.lora_table)

        add_lora_btn = QPushButton("＋  Add LoRA…")
        add_lora_btn.setObjectName("subtle")
        add_lora_btn.clicked.connect(self._add_lora)
        img_form.addRow("", add_lora_btn)

        layout.addWidget(img_box)

        self.image_model_input.textChanged.connect(self._update_detected_image_model)

        save_btn = QPushButton("Save App Settings")
        save_btn.setObjectName("accent")
        save_btn.clicked.connect(self._save)
        layout.addWidget(save_btn)



    def load(self, settings: AppSettings) -> None:
        self._settings = settings
        self.models_dir_input.setText(settings.models_directory)
        self.ctx_spin.setValue(settings.default_context_size)
        self.gpu_spin.setValue(settings.default_gpu_layers)
        self.threads_spin.setValue(settings.default_threads)
        self.threads_batch_spin.setValue(getattr(settings, "default_threads_batch", 0))
        self.autosave_check.setChecked(settings.auto_save)
        self.enable_thinking_check.setChecked(getattr(settings, "enable_thinking", False))
        self.allow_nsfw_check.setChecked(getattr(settings, "allow_nsfw", False))
        self.log_full_prompts_check.setChecked(getattr(settings, "log_full_prompts", False))
        self.ollama_url_input.setText(
            getattr(settings, "ollama_base_url", ollama_client.DEFAULT_OLLAMA_URL)
        )
        self.language_input.setText(settings.response_language)
        self.max_tokens_spin.setValue(getattr(settings, "content_max_tokens", 4000))
        self.system_prompt_input.setPlainText(settings.custom_system_prompt)
        self.moe_batch_spin.setValue(getattr(settings, "moe_n_batch", 1024))
        self.moe_ubatch_spin.setValue(getattr(settings, "moe_n_ubatch", 1024))

        self.image_model_input.setText(getattr(settings, "image_model_path", ""))
        self.image_text_encoder_input.setText(
            getattr(settings, "image_text_encoder_path", "")
        )
        self.image_vae_input.setText(getattr(settings, "image_vae_path", ""))
        self.image_output_dir_input.setText(getattr(settings, "image_output_directory", ""))
        self.image_width_spin.setValue(getattr(settings, "image_default_width", 512))
        self.image_height_spin.setValue(getattr(settings, "image_default_height", 512))
        self.image_steps_spin.setValue(getattr(settings, "image_default_steps", 20))
        self.image_cfg_spin.setValue(getattr(settings, "image_default_cfg_scale", 7.0))

        self._load_loras(getattr(settings, "image_loras", None) or [])

        backend_val = getattr(
            settings, "image_backend", ImageBackend.STABLE_DIFFUSION_CPP.value
        )
        for i in range(self.image_backend_combo.count()):
            if self.image_backend_combo.itemData(i) == backend_val:
                self.image_backend_combo.setCurrentIndex(i)
                break



    def _update_detected_image_model(self, path: str = "") -> None:
        path = path.strip()
        profile = get_image_model_profile(path)
        if not path:
            self.image_model_detected_label.setText("Detected model: —")
            return

        self.image_model_detected_label.setText(f"Detected model: {profile.label}")

    def _check_ollama(self) -> None:
        url = ollama_client.normalize_base_url(self.ollama_url_input.text())
        self.ollama_status_label.setText("Checking…")
        ok, message = ollama_client.check_server(url)
        color = "#3d9970" if ok else "#c0392b"
        self.ollama_status_label.setStyleSheet(f"color: {color}; font-size: 12px;")
        self.ollama_status_label.setText(("✓ " if ok else "✗ ") + message)

    def _browse_models_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Select Models Directory")
        if d:
            self.models_dir_input.setText(d)

    def _browse_image_model(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Diffusion Model",
            "",
            "Model Files (*.gguf *.safetensors *.ckpt *.bin);;All Files (*)",
        )
        if path:
            self.image_model_input.setText(path)

    def _browse_image_text_encoder(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Text Encoder / LLM",
            "",
            "Model Files (*.gguf *.safetensors);;All Files (*)",
        )
        if path:
            self.image_text_encoder_input.setText(path)

    def _browse_image_vae(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select VAE",
            "",
            "Model Files (*.safetensors *.gguf *.bin);;All Files (*)",
        )
        if path:
            self.image_vae_input.setText(path)

    def _browse_image_output_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Select Image Output Directory")
        if d:
            self.image_output_dir_input.setText(d)



    def _add_lora(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select LoRA Adapter",
            "",
            "LoRA Files (*.safetensors *.gguf *.bin);;All Files (*)",
        )
        if path:
            self._insert_lora_row(path, weight=0.8, enabled=True)

    def _insert_lora_row(
        self,
        path: str,
        weight: float = 0.8,
        enabled: bool = True,
        trigger: str = "",
    ) -> None:
        row = self.lora_table.rowCount()
        self.lora_table.insertRow(row)


        chk = QCheckBox()
        chk.setChecked(enabled)
        chk_container = QWidget()
        chk_layout = QHBoxLayout(chk_container)
        chk_layout.addWidget(chk)
        chk_layout.setAlignment(Qt.AlignCenter)
        chk_layout.setContentsMargins(0, 0, 0, 0)
        self.lora_table.setCellWidget(row, 0, chk_container)


        path_item = QTableWidgetItem(path)
        path_item.setToolTip(path)
        self.lora_table.setItem(row, 1, path_item)


        weight_spin = QDoubleSpinBox()
        weight_spin.setRange(-2.0, 2.0)
        weight_spin.setSingleStep(0.05)
        weight_spin.setDecimals(2)
        weight_spin.setValue(weight)
        weight_spin.setFixedWidth(80)
        weight_spin.setToolTip(
            "Adapter strength.\n"
            "Typical values: 0.5 – 1.0 (positive) or -0.5 – -1.0 (negative/inverse)."
        )
        self.lora_table.setCellWidget(row, 2, weight_spin)


        trigger_edit = QLineEdit()
        trigger_edit.setText(trigger)
        trigger_edit.setPlaceholderText("trigger word(s)…")
        trigger_edit.setToolTip(
            "Activation keywords required by this LoRA.\n"
            "These are automatically prepended to every prompt when the LoRA is enabled.\n"
            "Examples: 'ohwx person', 'in the style of xyz', 'detailed fantasy armor'"
        )
        self.lora_table.setCellWidget(row, 3, trigger_edit)


        rm_btn = QPushButton("✕")
        rm_btn.setObjectName("subtle")
        rm_btn.setFixedWidth(28)
        rm_btn.setToolTip("Remove this LoRA")
        rm_btn.clicked.connect(lambda _, r=row: self._remove_lora_row(r))
        self.lora_table.setCellWidget(row, 4, rm_btn)

        self.lora_table.resizeRowsToContents()

    def _remove_lora_row(self, row: int) -> None:

        btn = self.sender()
        if btn is None:
            return
        for r in range(self.lora_table.rowCount()):
            widget = self.lora_table.cellWidget(r, 4)
            if widget is btn:
                self.lora_table.removeRow(r)

                for new_r in range(self.lora_table.rowCount()):
                    new_btn = self.lora_table.cellWidget(new_r, 4)
                    if new_btn:
                        try:
                            new_btn.clicked.disconnect()
                        except RuntimeError:
                            pass
                        new_btn.clicked.connect(lambda _, rr=new_r: self._remove_lora_row(rr))
                return

    def _collect_loras(self) -> list:
        loras = []
        for r in range(self.lora_table.rowCount()):
            path_item = self.lora_table.item(r, 1)
            if path_item is None:
                continue
            path = path_item.text().strip()
            if not path:
                continue

            chk_container = self.lora_table.cellWidget(r, 0)
            enabled = True
            if chk_container:
                chk = chk_container.findChild(QCheckBox)
                if chk:
                    enabled = chk.isChecked()

            weight_spin = self.lora_table.cellWidget(r, 2)
            weight = 0.8
            if weight_spin and isinstance(weight_spin, QDoubleSpinBox):
                weight = weight_spin.value()

            trigger_edit = self.lora_table.cellWidget(r, 3)
            trigger = ""
            if trigger_edit and isinstance(trigger_edit, QLineEdit):
                trigger = trigger_edit.text().strip()

            loras.append({"path": path, "weight": weight, "enabled": enabled, "trigger": trigger})
        return loras

    def _load_loras(self, loras: list) -> None:
        self.lora_table.setRowCount(0)
        for entry in (loras or []):
            self._insert_lora_row(
                entry.get("path", ""),
                weight=float(entry.get("weight", 0.8)),
                enabled=bool(entry.get("enabled", True)),
                trigger=entry.get("trigger", ""),
            )



    def _save(self) -> None:
        if self._settings is None:
            self._settings = AppSettings()
        self._settings.models_directory = self.models_dir_input.text().strip()
        self._settings.default_context_size = self.ctx_spin.value()
        self._settings.default_gpu_layers = self.gpu_spin.value()
        self._settings.default_threads = self.threads_spin.value()
        self._settings.default_threads_batch = self.threads_batch_spin.value()
        self._settings.auto_save = self.autosave_check.isChecked()
        self._settings.enable_thinking = self.enable_thinking_check.isChecked()
        self._settings.allow_nsfw = self.allow_nsfw_check.isChecked()
        self._settings.log_full_prompts = self.log_full_prompts_check.isChecked()
        self._settings.ollama_base_url = ollama_client.normalize_base_url(
            self.ollama_url_input.text()
        )
        self._settings.response_language = self.language_input.text().strip()
        self._settings.content_max_tokens = self.max_tokens_spin.value()
        self._settings.custom_system_prompt = self.system_prompt_input.toPlainText().strip()
        self._settings.moe_n_batch = self.moe_batch_spin.value()
        self._settings.moe_n_ubatch = self.moe_ubatch_spin.value()

        self._settings.image_model_path = self.image_model_input.text().strip()
        self._settings.image_text_encoder_path = (
            self.image_text_encoder_input.text().strip()
        )
        self._settings.image_vae_path = self.image_vae_input.text().strip()
        self._settings.image_backend = (
            self.image_backend_combo.currentData()
            or ImageBackend.STABLE_DIFFUSION_CPP.value
        )
        self._settings.image_output_directory = (
            self.image_output_dir_input.text().strip()
        )
        self._settings.image_default_width = self.image_width_spin.value()
        self._settings.image_default_height = self.image_height_spin.value()
        self._settings.image_default_steps = self.image_steps_spin.value()
        self._settings.image_default_cfg_scale = self.image_cfg_spin.value()
        self._settings.image_loras = self._collect_loras()
        storage.save_settings(self._settings)
        self.settings_changed.emit(self._settings)

    def get_models_directory(self) -> str:
        return self.models_dir_input.text().strip()


class ModelsPanel(QWidget):
    """
    The Models tab — assigns a GGUF model to each task for the current project.
    """

    assignments_changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._project: Optional[Project] = None
        self._available_models: list[str] = []
        self._pickers: dict[TaskType, ModelPicker] = {}
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 20)
        outer.setSpacing(16)

        title = QLabel("Model Assignments")
        title.setObjectName("heading")
        outer.addWidget(title)

        subtitle = QLabel(
            "Each task can use a different model. Models tagged [llama.cpp] "
            "are GGUF files run locally; models tagged [Ollama] run on your "
            "Ollama server. The Manager Agent loads the correct model automatically."
        )
        subtitle.setObjectName("subheading")
        subtitle.setWordWrap(True)
        outer.addWidget(subtitle)


        refresh_row = QHBoxLayout()
        self.dir_label = QLabel("GGUF folder (llama.cpp): not set. Configure in Settings.")
        self.dir_label.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 12px;")
        refresh_row.addWidget(self.dir_label, 1)
        refresh_btn = QPushButton("Refresh Models")
        refresh_btn.clicked.connect(self._refresh_models)
        refresh_row.addWidget(refresh_btn)
        outer.addLayout(refresh_row)

        self.ollama_label = QLabel("Ollama: not checked yet.")
        self.ollama_label.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 12px;")
        self.ollama_label.setWordWrap(True)
        outer.addWidget(self.ollama_label)


        box = QGroupBox("Task → Model Assignment")
        box_layout = QVBoxLayout(box)
        box_layout.setSpacing(10)
        box_layout.setContentsMargins(14, 18, 14, 14)

        for task in TaskType:
            picker = ModelPicker(task, self._available_models)
            picker.model_changed.connect(self._on_model_changed)
            picker.temperature_changed.connect(self._on_temperature_changed)
            picker.top_p_changed.connect(self._on_top_p_changed)
            picker.top_k_changed.connect(self._on_top_k_changed)
            self._pickers[task] = picker
            box_layout.addWidget(picker)

        outer.addWidget(box)


        action_box = QGroupBox("Quick Actions")
        ab_layout = QVBoxLayout(action_box)
        ab_layout.setContentsMargins(14, 18, 14, 14)
        ab_layout.setSpacing(8)

        assign_all_row = QHBoxLayout()
        assign_all_lbl = QLabel("Assign one model to all tasks:")
        assign_all_lbl.setStyleSheet(f"color: {COLOR_TEXT_DIM};")
        assign_all_row.addWidget(assign_all_lbl)
        self.assign_all_combo = QComboBox()
        self.assign_all_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.assign_all_combo.addItem("— select model —", "")
        assign_all_row.addWidget(self.assign_all_combo, 1)
        assign_all_btn = QPushButton("Assign to All")
        assign_all_btn.clicked.connect(self._assign_to_all)
        assign_all_row.addWidget(assign_all_btn)
        ab_layout.addLayout(assign_all_row)

        params_row = QHBoxLayout()
        params_lbl = QLabel("Parameters for all:")
        params_lbl.setStyleSheet(f"color: {COLOR_TEXT_DIM};")
        params_row.addWidget(params_lbl)

        params_row.addWidget(QLabel("Temp"))
        self.assign_all_temp_spin = QDoubleSpinBox()
        self.assign_all_temp_spin.setRange(0.0, 2.0)
        self.assign_all_temp_spin.setSingleStep(0.05)
        self.assign_all_temp_spin.setDecimals(2)
        self.assign_all_temp_spin.setValue(0.7)
        self.assign_all_temp_spin.setFixedWidth(70)
        self.assign_all_temp_spin.setToolTip(
            "Temperature to apply to every task when clicking Assign to All."
        )
        params_row.addWidget(self.assign_all_temp_spin)

        params_row.addWidget(QLabel("Top P"))
        self.assign_all_top_p_spin = QDoubleSpinBox()
        self.assign_all_top_p_spin.setRange(0.0, 1.0)
        self.assign_all_top_p_spin.setSingleStep(0.05)
        self.assign_all_top_p_spin.setDecimals(2)
        self.assign_all_top_p_spin.setValue(0.9)
        self.assign_all_top_p_spin.setFixedWidth(70)
        self.assign_all_top_p_spin.setToolTip(
            "Top P to apply to every task when clicking Assign to All."
        )
        params_row.addWidget(self.assign_all_top_p_spin)

        params_row.addWidget(QLabel("Top K"))
        self.assign_all_top_k_spin = QSpinBox()
        self.assign_all_top_k_spin.setRange(0, 1000)
        self.assign_all_top_k_spin.setSingleStep(1)
        self.assign_all_top_k_spin.setValue(40)
        self.assign_all_top_k_spin.setFixedWidth(70)
        self.assign_all_top_k_spin.setToolTip(
            "Top K to apply to every task when clicking Assign to All. 0 = disabled/no limit."
        )
        params_row.addWidget(self.assign_all_top_k_spin)
        params_row.addStretch()
        ab_layout.addLayout(params_row)

        outer.addWidget(action_box)
        outer.addStretch()

    def load_project(self, project: Project) -> None:
        self._project = project
        for task, picker in self._pickers.items():
            picker.set_value(project.model_assignments.get(task))
            picker.set_temperature(project.task_temperatures.get(task))
            picker.set_top_p(project.task_temperatures.get_top_p(task))
            picker.set_top_k(project.task_temperatures.get_top_k(task))

        first_task = next(iter(self._pickers), None)
        if first_task is not None:
            first_picker = self._pickers[first_task]
            self.assign_all_temp_spin.setValue(first_picker.get_temperature())
            self.assign_all_top_p_spin.setValue(first_picker.get_top_p())
            self.assign_all_top_k_spin.setValue(first_picker.get_top_k())

    def update_available_models(self, models_dir: str, ollama_base_url: str = "") -> None:
        gguf_models = storage.list_gguf_models(models_dir) if models_dir else []
        self.dir_label.setText(
            f"GGUF folder (llama.cpp): {models_dir} — "
            f"{len(gguf_models)} model{'s' if len(gguf_models) != 1 else ''} found"
            if models_dir
            else "GGUF folder (llama.cpp): not set. Configure in Settings."
        )

        ollama_url = ollama_client.normalize_base_url(ollama_base_url)
        try:
            ollama_names = ollama_client.list_models(ollama_url)
            ollama_refs = [ollama_client.make_ollama_ref(n) for n in ollama_names]
            self.ollama_label.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 12px;")
            self.ollama_label.setText(
                f"Ollama ({ollama_url}): {len(ollama_refs)} model"
                f"{'s' if len(ollama_refs) != 1 else ''} available"
            )
        except ollama_client.OllamaError as e:
            ollama_refs = []
            self.ollama_label.setStyleSheet("color: #c0392b; font-size: 12px;")
            self.ollama_label.setText(f"Ollama not reachable — {e}")

        models = gguf_models + ollama_refs
        self._available_models = models

        for picker in self._pickers.values():
            picker.update_models(models)

        self.assign_all_combo.clear()
        self.assign_all_combo.addItem("— select model —", "")
        for m in models:
            self.assign_all_combo.addItem(_model_label(m), m)

    def _refresh_models(self) -> None:
        settings = storage.load_settings()
        self.update_available_models(settings.models_directory, settings.ollama_base_url)

    def _on_model_changed(self, task: TaskType, path: str) -> None:
        if not self._project:
            return
        self._project.model_assignments.set(task, path)
        storage.save_project(self._project)
        self.assignments_changed.emit()

    def _on_temperature_changed(self, task: TaskType, value: float) -> None:
        if not self._project:
            return
        self._project.task_temperatures.set(task, value)
        storage.save_project(self._project)
        self.assignments_changed.emit()

    def _on_top_p_changed(self, task: TaskType, value: float) -> None:
        if not self._project:
            return
        self._project.task_temperatures.set_top_p(task, value)
        storage.save_project(self._project)
        self.assignments_changed.emit()

    def _on_top_k_changed(self, task: TaskType, value: int) -> None:
        if not self._project:
            return
        self._project.task_temperatures.set_top_k(task, value)
        storage.save_project(self._project)
        self.assignments_changed.emit()

    def _assign_to_all(self) -> None:
        path = self.assign_all_combo.currentData() or ""
        if not path:
            return

        temperature = self.assign_all_temp_spin.value()
        top_p = self.assign_all_top_p_spin.value()
        top_k = self.assign_all_top_k_spin.value()

        # One click applies the selected model and the three generation
        # parameters to every task. Per-task values remain stored separately,
        # but the common workflow is configured from one place.
        for task, picker in self._pickers.items():
            picker.set_value(path)
            picker.set_temperature(temperature)
            picker.set_top_p(top_p)
            picker.set_top_k(top_k)

            if self._project:
                self._project.model_assignments.set(task, path)
                self._project.task_temperatures.set(task, temperature)
                self._project.task_temperatures.set_top_p(task, top_p)
                self._project.task_temperatures.set_top_k(task, top_k)

        if self._project:
            storage.save_project(self._project)
        self.assignments_changed.emit()


class SettingsPanel(QWidget):
    """
    Application settings panel (separate from per-project model assignments).
    """

    settings_changed = Signal(AppSettings)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)

        inner = QWidget()
        inner_layout = QVBoxLayout(inner)
        inner_layout.setContentsMargins(20, 20, 20, 20)
        inner_layout.setSpacing(16)

        title = QLabel("Settings")
        title.setObjectName("heading")
        inner_layout.addWidget(title)

        self.app_settings_widget = AppSettingsWidget()
        self.app_settings_widget.settings_changed.connect(self.settings_changed)
        inner_layout.addWidget(self.app_settings_widget)


        notice_box = QGroupBox("Local Runtimes")
        nb_layout = QVBoxLayout(notice_box)
        nb_layout.setContentsMargins(14, 18, 14, 14)
        nb_layout.setSpacing(12)

        try:
            from llama_cpp import Llama
            llama_status = "✓ llama.cpp (llama-cpp-python) is installed — GGUF models can run."
            llama_color = "#3d9970"
        except ImportError:
            llama_status = (
                "✗ llama.cpp (llama-cpp-python) is not installed — GGUF models cannot run.\n\n"
                "Install it with:\n"
                "  pip install llama-cpp-python\n\n"
                "For GPU support:\n"
                "  CMAKE_ARGS=\"-DLLAMA_CUBLAS=on\" pip install llama-cpp-python --upgrade"
            )
            llama_color = "#c0392b"

        llama_lbl = QLabel(llama_status)
        llama_lbl.setStyleSheet(f"color: {llama_color}; font-size: 13px;")
        llama_lbl.setWordWrap(True)
        nb_layout.addWidget(llama_lbl)

        ollama_hint = QLabel(
            "Ollama (for [Ollama] models) is not a Python package. It runs as a "
            "separate program; check it with the Ollama section above."
        )
        ollama_hint.setWordWrap(True)
        ollama_hint.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 12px;")
        nb_layout.addWidget(ollama_hint)
        inner_layout.addWidget(notice_box)

        inner_layout.addStretch()

        scroll.setWidget(inner)
        outer.addWidget(scroll)

    def load(self, settings: AppSettings) -> None:
        self.app_settings_widget.load(settings)