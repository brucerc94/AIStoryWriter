"""
Core data models for AI Story Studio.
All entities are plain dataclasses — no ORM, no database.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class ImageTaskType(str, Enum):
    """Types of image generation tasks available in the Images tab."""
    CHARACTER_PORTRAIT = "character_portrait"
    BOOK_COVER = "book_cover"
    SCENE_ILLUSTRATION = "scene_illustration"
    LOCATION = "location"
    OBJECT_ITEM = "object_item"


class ImageBackend(str, Enum):
    """Supported image generation backends. Designed for easy extension."""
    STABLE_DIFFUSION_CPP = "stable_diffusion_cpp"
    # Future backends: FLUX = "flux", QWEN_IMAGE = "qwen_image"


class ImageGenerationRequest:
    """
    Data transfer object for an image generation request.
    Passed from ImageController → ImageWorkflow → ImageEngine.
    All fields are optional — the engine fills defaults for unset ones.
    """
    def __init__(
        self,
        task_type: ImageTaskType,
        prompt: str = "",
        negative_prompt: str = "",
        seed: int = -1,
        width: int = 512,
        height: int = 512,
        steps: int = 20,
        cfg_scale: float = 7.0,
    ) -> None:
        self.task_type = task_type
        self.prompt = prompt
        self.negative_prompt = negative_prompt
        self.seed = seed          # -1 = random
        self.width = width
        self.height = height
        self.steps = steps
        self.cfg_scale = cfg_scale


class ImageGenerationResult:
    """
    Result returned by ImageEngine after generation (or failure).
    image_path is set only on success; error_message only on failure.
    """
    def __init__(
        self,
        success: bool,
        image_path: str = "",
        error_message: str = "",
        seed_used: int = -1,
    ) -> None:
        self.success = success
        self.image_path = image_path
        self.error_message = error_message
        self.seed_used = seed_used


class TaskType(str, Enum):
    WRITE_SYNOPSIS = "write_synopsis"
    GENERATE_OUTLINE = "generate_outline"
    REVIEW_OUTLINE = "review_outline"
    GENERATE_WORLD = "generate_world"
    WRITE_CHAPTER = "write_chapter"
    WRITE_BOOK = "write_book"
    REVIEW_CHAPTER = "review_chapter"
    REWRITE_CHAPTER = "rewrite_chapter"
    CHANGE_CHAPTER = "change_chapter"
    UPDATE_MEMORY = "update_memory"
    CONVERSATION_SUMMARY = "conversation_summary"
    CHAT = "chat"


class WorkflowStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    DONE = "done"
    ERROR = "error"


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    SUMMARY = "summary"


@dataclass
class ChatMessage:
    role: MessageRole
    content: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    # If True this message was summarized and is no longer sent to the model
    summarized: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "role": self.role.value,
            "content": self.content,
            "timestamp": self.timestamp,
            "summarized": self.summarized,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ChatMessage":
        return cls(
            id=d.get("id", str(uuid.uuid4())),
            role=MessageRole(d["role"]),
            content=d["content"],
            timestamp=d.get("timestamp", datetime.now().isoformat()),
            summarized=d.get("summarized", False),
        )


@dataclass
class Character:
    name: str
    role: str
    description: str
    backstory: str = ""
    traits: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    image_ref: Optional[dict[str, Any]] = None
    image_status: str = "No Image"
    image_error: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "description": self.description,
            "backstory": self.backstory,
            "traits": self.traits,
            "image_ref": self.image_ref,
            "image_status": self.image_status,
            "image_error": self.image_error,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Character":
        return cls(
            id=d.get("id", str(uuid.uuid4())),
            name=d["name"],
            role=d.get("role", ""),
            description=d.get("description", ""),
            backstory=d.get("backstory", ""),
            traits=d.get("traits", []),
            image_ref=d.get("image_ref"),
            image_status=d.get("image_status", "No Image"),
            image_error=d.get("image_error", ""),
        )

    def to_markdown(self) -> str:
        lines = [
            f"## {self.name}",
            f"**Role:** {self.role}",
            "",
            f"**Description:** {self.description}",
            "",
        ]
        if self.backstory:
            lines += [f"**Backstory:** {self.backstory}", ""]
        if self.traits:
            lines += ["**Traits:**"]
            for t in self.traits:
                lines.append(f"- {t}")
            lines.append("")
        return "\n".join(lines)


@dataclass
class Chapter:
    number: int
    title: str
    summary: str = ""
    content: str = ""
    reviewed: bool = False
    last_review: str = ""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "number": self.number,
            "title": self.title,
            "summary": self.summary,
            "content": self.content,
            "reviewed": self.reviewed,
            "last_review": self.last_review,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Chapter":
        return cls(
            id=d.get("id", str(uuid.uuid4())),
            number=d["number"],
            title=d.get("title", f"Chapter {d['number']}"),
            summary=d.get("summary", ""),
            content=d.get("content", ""),
            reviewed=d.get("reviewed", False),
            last_review=d.get("last_review", ""),
        )


@dataclass
class ModelAssignment:
    """Maps each task type to a GGUF model path."""
    write_synopsis: str = ""
    generate_outline: str = ""
    review_outline: str = ""
    generate_world: str = ""
    write_chapter: str = ""
    review_chapter: str = ""
    rewrite_chapter: str = ""
    update_memory: str = ""
    conversation_summary: str = ""
    chat: str = ""

    def get(self, task: TaskType) -> str:
        if task == TaskType.WRITE_BOOK:
            return self.write_chapter
        return getattr(self, task.value, "")

    def set(self, task: TaskType, model_path: str) -> None:
        setattr(self, task.value, model_path)

    def to_dict(self) -> dict:
        return {
            "write_synopsis": self.write_synopsis,
            "generate_outline": self.generate_outline,
            "review_outline": self.review_outline,
            "generate_world": self.generate_world,
            "write_chapter": self.write_chapter,
            "review_chapter": self.review_chapter,
            "rewrite_chapter": self.rewrite_chapter,
            "update_memory": self.update_memory,
            "conversation_summary": self.conversation_summary,
            "chat": self.chat,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ModelAssignment":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class ImageModelAssignment:
    """
    Maps image task types to a model path (GGUF or diffusion checkpoint).
    Follows the exact same pattern as ModelAssignment so the Settings UI
    can reuse ModelPicker without modification.

    A single ``default`` path acts as the catch-all model for all image
    tasks — individual task overrides can be added later without changing
    the architecture.
    """
    default: str = ""
    character_portrait: str = ""
    book_cover: str = ""
    scene_illustration: str = ""
    location: str = ""
    object_item: str = ""

    def get(self, task: "ImageTaskType") -> str:  # noqa: F821
        specific = getattr(self, task.value, "")
        return specific or self.default

    def set(self, task: "ImageTaskType", model_path: str) -> None:  # noqa: F821
        setattr(self, task.value, model_path)

    def to_dict(self) -> dict:
        return {
            "default": self.default,
            "character_portrait": self.character_portrait,
            "book_cover": self.book_cover,
            "scene_illustration": self.scene_illustration,
            "location": self.location,
            "object_item": self.object_item,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ImageModelAssignment":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class TaskTemperatures:
    """
    Per-task generation temperature, set from the Models tab right next to
    each task's assigned GGUF model. Different models often need very
    different temperatures (a small fast model for summaries vs. a large
    creative model for chapters), so this lives per-task rather than as a
    single global value.
    """
    write_synopsis: float = 0.7
    generate_outline: float = 0.7
    review_outline: float = 0.7
    generate_world: float = 0.7
    write_chapter: float = 0.8
    review_chapter: float = 0.7
    rewrite_chapter: float = 0.7
    update_memory: float = 0.7
    conversation_summary: float = 0.3
    chat: float = 0.7

    def get(self, task: TaskType) -> float:
        if task == TaskType.WRITE_BOOK:
            return self.write_chapter
        return getattr(self, task.value, 0.7)

    def set(self, task: TaskType, temperature: float) -> None:
        setattr(self, task.value, temperature)

    def to_dict(self) -> dict:
        return {
            "write_synopsis": self.write_synopsis,
            "generate_outline": self.generate_outline,
            "review_outline": self.review_outline,
            "generate_world": self.generate_world,
            "write_chapter": self.write_chapter,
            "review_chapter": self.review_chapter,
            "rewrite_chapter": self.rewrite_chapter,
            "update_memory": self.update_memory,
            "conversation_summary": self.conversation_summary,
            "chat": self.chat,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TaskTemperatures":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class AuthorIntent:
    """
    The author's high-level creative goals for this book.

    Answers "why" and "what for", not "what happens" — that is the
    synopsis's job. These fields complement the synopsis; they do not
    duplicate it. All fields are optional: leave empty and the model
    infers from the synopsis and genre.

    Persisted in project.json. Available to every task that builds a
    prompt (outline, chapters, review, rewrite) via project.author_intent.
    """
    # What emotional arc should the reader experience while reading?
    emotional_journey: str = ""
    # What should the reader feel or remember long after the last page?
    lasting_impression: str = ""
    # What themes or ideas does this book want to explore?
    themes: str = ""
    # What makes this book distinct — voice, structure, perspective?
    unique_elements: str = ""
    # Works that inspired this book, and what aspect of them (optional).
    inspirations: str = ""
    # Subjects, tropes, or tones to avoid entirely.
    avoid: str = ""

    # ── Prompt rendering ──────────────────────────────────────────────

    def is_empty(self) -> bool:
        return not any([
            self.emotional_journey, self.lasting_impression,
            self.themes, self.unique_elements,
            self.inspirations, self.avoid,
        ])

    def to_prompt_fragment(self) -> str:
        """
        Renders only the fields the author filled in, as concise model
        instructions. Returns "" when the profile is empty so callers
        can skip the whole section without adding an empty header.
        """
        if self.is_empty():
            return ""
        lines = []
        if self.emotional_journey:
            lines.append(f"Emotional journey for the reader: {self.emotional_journey}")
        if self.lasting_impression:
            lines.append(f"What the reader should remember after finishing: {self.lasting_impression}")
        if self.themes:
            lines.append(f"Core themes to explore: {self.themes}")
        if self.unique_elements:
            lines.append(f"What makes this book unique: {self.unique_elements}")
        if self.inspirations:
            lines.append(f"Inspirations (and what aspect): {self.inspirations}")
        if self.avoid:
            lines.append(f"Avoid entirely: {self.avoid}")
        return "\n".join(lines)

    # ── Serialization ─────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "emotional_journey": self.emotional_journey,
            "lasting_impression": self.lasting_impression,
            "themes": self.themes,
            "unique_elements": self.unique_elements,
            "inspirations": self.inspirations,
            "avoid": self.avoid,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AuthorIntent":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class WritingStyle:
    """
    Technical and stylistic preferences for how the book is written.

    Answers "how it reads", not "what it says". All fields use a small
    controlled vocabulary so the prompt fragment stays concise. Empty
    field = let the model infer from the synopsis and genre context.

    Persisted in project.json alongside AuthorIntent.
    """
    # Narrative point of view
    narrator_pov: str = ""        # "first person" | "third limited" | "third omniscient" | "second person"
    # Chapter-level rhythm
    pacing: str = ""              # "fast" | "moderate" | "slow" | "variable"
    # Amount of descriptive prose vs. action/dialogue
    description_density: str = "" # "sparse" | "balanced" | "rich"
    # Dialogue presence
    dialogue_style: str = ""      # "frequent" | "moderate" | "minimal"
    # How graphic violence is handled
    violence_level: str = ""      # "none" | "implied" | "moderate" | "explicit"
    # Centrality of romance to the story
    romance_level: str = ""       # "none" | "background" | "subplot" | "central"
    # Approximate prose length per chapter
    target_chapter_length: str = "" # "short (~1k words)" | "medium (~2k words)" | "long (~3k+ words)"
    # Only fill if the genre is genuinely ambiguous from the synopsis
    genre_tags: str = ""          # e.g. "psychological thriller, neo-noir"

    # ── Prompt rendering ──────────────────────────────────────────────

    def is_empty(self) -> bool:
        return not any([
            self.narrator_pov, self.pacing, self.description_density,
            self.dialogue_style, self.violence_level, self.romance_level,
            self.target_chapter_length, self.genre_tags,
        ])

    def to_prompt_fragment(self) -> str:
        """
        Renders only specified preferences as concise model instructions.
        Returns "" when nothing has been set.
        """
        if self.is_empty():
            return ""
        lines = []
        if self.genre_tags:
            lines.append(f"Genre: {self.genre_tags}.")
        if self.narrator_pov:
            lines.append(f"Narrative point of view: {self.narrator_pov}.")
        if self.pacing:
            lines.append(f"Overall pacing: {self.pacing}.")
        if self.description_density:
            lines.append(f"Description density: {self.description_density}.")
        if self.dialogue_style:
            lines.append(f"Dialogue style: {self.dialogue_style}.")
        if self.violence_level:
            lines.append(f"Violence: {self.violence_level}.")
        if self.romance_level:
            lines.append(f"Romance: {self.romance_level}.")
        if self.target_chapter_length:
            lines.append(f"Target chapter length: {self.target_chapter_length}.")
        return "\n".join(lines)

    # ── Serialization ─────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "narrator_pov": self.narrator_pov,
            "pacing": self.pacing,
            "description_density": self.description_density,
            "dialogue_style": self.dialogue_style,
            "violence_level": self.violence_level,
            "romance_level": self.romance_level,
            "target_chapter_length": self.target_chapter_length,
            "genre_tags": self.genre_tags,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "WritingStyle":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class Project:
    title: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    synopsis: str = ""
    outline: str = ""
    world: str = ""
    memory: str = ""
    characters: list[Character] = field(default_factory=list)
    chapters: list[Chapter] = field(default_factory=list)
    chat_messages: list[ChatMessage] = field(default_factory=list)
    chat_summary: str = ""
    model_assignments: ModelAssignment = field(default_factory=ModelAssignment)
    task_temperatures: TaskTemperatures = field(default_factory=TaskTemperatures)
    # Creative intent and style — persist with the project, used by all tasks.
    author_intent: AuthorIntent = field(default_factory=AuthorIntent)
    writing_style: WritingStyle = field(default_factory=WritingStyle)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    workflow_status: WorkflowStatus = WorkflowStatus.IDLE
    current_chapter: int = 0
    # How many recent messages to always keep unsummarized
    recent_message_window: int = 20

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "synopsis": self.synopsis,
            "outline": self.outline,
            "world": self.world,
            "memory": self.memory,
            "chat_summary": self.chat_summary,
            "model_assignments": self.model_assignments.to_dict(),
            "task_temperatures": self.task_temperatures.to_dict(),
            "author_intent": self.author_intent.to_dict(),
            "writing_style": self.writing_style.to_dict(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "workflow_status": self.workflow_status.value,
            "current_chapter": self.current_chapter,
            "recent_message_window": self.recent_message_window,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Project":
        ma = ModelAssignment.from_dict(d.get("model_assignments", {}))
        tt = TaskTemperatures.from_dict(d.get("task_temperatures", {}))
        ai = AuthorIntent.from_dict(d.get("author_intent", {}))
        ws = WritingStyle.from_dict(d.get("writing_style", {}))
        return cls(
            id=d.get("id", str(uuid.uuid4())),
            title=d["title"],
            synopsis=d.get("synopsis", ""),
            outline=d.get("outline", ""),
            world=d.get("world", ""),
            memory=d.get("memory", ""),
            chat_summary=d.get("chat_summary", ""),
            model_assignments=ma,
            task_temperatures=tt,
            author_intent=ai,
            writing_style=ws,
            created_at=d.get("created_at", datetime.now().isoformat()),
            updated_at=d.get("updated_at", datetime.now().isoformat()),
            workflow_status=WorkflowStatus(d.get("workflow_status", "idle")),
            current_chapter=d.get("current_chapter", 0),
            recent_message_window=d.get("recent_message_window", 20),
        )


@dataclass
class AppSettings:
    models_directory: str = ""
    default_context_size: int = 4096
    default_gpu_layers: int = 0
    default_threads: int = 4
    # Threads used for prompt/batch processing (llama.cpp's n_threads_batch),
    # separate from default_threads which controls single-token generation.
    # 0 = auto (let llama-cpp-python mirror default_threads, its own default
    # behavior when this kwarg isn't passed at all).
    default_threads_batch: int = 0
    theme: str = "dark"
    font_size: int = 13
    auto_save: bool = True
    # Appended to the end of every auto-generated system prompt (all tasks),
    # so the author can add persistent style/content instructions without
    # editing code — e.g. tone guidance, content-rating notes, POV rules.
    custom_system_prompt: str = ""
    # Overrides the per-task default temperature everywhere (chat, chapter
    # writing, review, memory updates, summarization). 0.0 = deterministic,
    # 2.0 = max randomness.
    temperature: float = 0.7
    # Language the model should respond in, regardless of what language the
    # (English) built-in instructions are written in. Empty = no explicit
    # instruction (model picks based on context, usually English).
    response_language: str = "Español"
    # Only applied automatically to Qwen models that expose the
    # enable_thinking chat template toggle in the installed llama-cpp-python.
    enable_thinking: bool = False
    # Max output tokens requested per generation PASS for the long-form
    # content-writing tasks: outline generation, chapter writing, and
    # chapter rewriting (WRITE_BOOK just calls chapter writing per chapter,
    # so it follows this too). This is NOT the same as Context Size above —
    # Context Size is the model's total window (prompt + reply combined);
    # this is how much reply we ask for in one go before the continuation
    # loop kicks in to keep going. Raising it means fewer continuation
    # passes per chapter/outline, but each pass needs that much reply room
    # left over within Context Size, so it's capped there automatically.
    content_max_tokens: int = 4000
    # When enabled, the model is explicitly told it may write adult/mature
    # content without restrictions — violence, sexuality, profanity, dark
    # themes — and that no content filtering is applied. Only affects the
    # creative-writing tasks (synopsis, outline, chapters, world, memory,
    # characters); review/chat/summary tasks are unaffected.
    allow_nsfw: bool = False
    # Batch/micro-batch size used ONLY when a MoE model is detected (see
    # engine/chat.py) — larger batches amortize per-token expert-routing
    # overhead better than dense models. Ignored entirely for dense models,
    # and only ever applied if the installed llama-cpp-python build actually
    # supports the n_batch/n_ubatch kwargs (checked via engine.llama_features).
    moe_n_batch: int = 1024
    moe_n_ubatch: int = 1024
    # When enabled, every call to the model logs the FULL system+user prompt
    # content (not just token counts) at INFO level, so it shows up in the
    # console/log file. Off by default — full prompts can be long and noisy
    # for normal use; turn this on only when you need to see exactly what's
    # being sent, e.g. while debugging.
    log_full_prompts: bool = False

    # ── Image generation ──────────────────────────────────────────────────
    # These settings are completely independent of the text-generation
    # pipeline above. The image engine reads them; the story workflow ignores
    # them entirely.

    # Path to the diffusion model (standalone diffusion checkpoint / GGUF).
    # For Z-Image-Turbo: z_image_turbo-Q4_0.gguf
    # For monolithic checkpoints (SD 1.x, SDXL, …): the full model file.
    image_model_path: str = ""

    # Optional: path to a standalone text encoder / LLM used by the image
    # model. Required by multi-component architectures like Z-Image-Turbo
    # (Qwen3-4B-ZImage-Heretic-Genesis-Q8.gguf) and Flux 2
    # (Mistral-Small-3.2). Leave empty for monolithic checkpoints.
    image_text_encoder_path: str = ""

    # Optional: path to a standalone VAE. Required by multi-component
    # architectures (Z-Image-Turbo: ae.safetensors, Flux: ae.safetensors).
    # Leave empty when the VAE is baked into the main checkpoint.
    image_vae_path: str = ""

    # Which backend to use for image generation.
    image_backend: str = ImageBackend.STABLE_DIFFUSION_CPP.value

    # Directory where generated images are saved.
    image_output_directory: str = ""

    # Default image dimensions (can be overridden per-request in the UI).
    image_default_width: int = 512
    image_default_height: int = 512

    # Default sampling steps.
    image_default_steps: int = 20

    # Default CFG scale.
    image_default_cfg_scale: float = 7.0

    # LoRA models for image generation.
    # Each entry is a dict: {"path": str, "weight": float, "enabled": bool}
    # weight range: -2.0 to 2.0, typical values 0.5 – 1.0
    # enabled: False means the entry is stored but skipped during generation.
    image_loras: list = None  # type: list[dict]

    def __post_init__(self):
        if self.image_loras is None:
            self.image_loras = []

    def to_dict(self) -> dict:
        return {
            "models_directory": self.models_directory,
            "default_context_size": self.default_context_size,
            "default_gpu_layers": self.default_gpu_layers,
            "default_threads": self.default_threads,
            "default_threads_batch": self.default_threads_batch,
            "theme": self.theme,
            "font_size": self.font_size,
            "auto_save": self.auto_save,
            "custom_system_prompt": self.custom_system_prompt,
            "temperature": self.temperature,
            "response_language": self.response_language,
            "enable_thinking": self.enable_thinking,
            "content_max_tokens": self.content_max_tokens,
            "allow_nsfw": self.allow_nsfw,
            "moe_n_batch": self.moe_n_batch,
            "moe_n_ubatch": self.moe_n_ubatch,
            "log_full_prompts": self.log_full_prompts,
            # Image generation
            "image_model_path": self.image_model_path,
            "image_text_encoder_path": self.image_text_encoder_path,
            "image_vae_path": self.image_vae_path,
            "image_backend": self.image_backend,
            "image_output_directory": self.image_output_directory,
            "image_default_width": self.image_default_width,
            "image_default_height": self.image_default_height,
            "image_default_steps": self.image_default_steps,
            "image_default_cfg_scale": self.image_default_cfg_scale,
            "image_loras": self.image_loras if self.image_loras is not None else [],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AppSettings":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})