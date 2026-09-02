"""
Core data models for AI Story Studio.
All entities are plain dataclasses — no ORM, no database.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
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
    """Supported local image generation backends."""
    STABLE_DIFFUSION_CPP = "stable_diffusion_cpp"


class ImageGenerationRequest:
    """Data transfer object for a local image generation request."""
    def __init__(self, task_type: ImageTaskType, prompt: str = "", negative_prompt: str = "", seed: int = -1, width: int = 512, height: int = 512, steps: int = 20, cfg_scale: float = 7.0) -> None:
        self.task_type = task_type
        self.prompt = prompt
        self.negative_prompt = negative_prompt
        self.seed = seed
        self.width = width
        self.height = height
        self.steps = steps
        self.cfg_scale = cfg_scale


class ImageGenerationResult:
    """Result returned by ImageEngine after generation."""
    def __init__(self, success: bool, image_path: str = "", error_message: str = "", seed_used: int = -1) -> None:
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
    summarized: bool = False

    def to_dict(self) -> dict:
        return {"id": self.id, "role": self.role.value, "content": self.content, "timestamp": self.timestamp, "summarized": self.summarized}

    @classmethod
    def from_dict(cls, d: dict) -> "ChatMessage":
        return cls(id=d.get("id", str(uuid.uuid4())), role=MessageRole(d["role"]), content=d["content"], timestamp=d.get("timestamp", datetime.now().isoformat()), summarized=d.get("summarized", False))


@dataclass
class CharacterRelationship:
    """A directed relationship from one character to another.

    Both fields are required:
      - related_character: the name of the other character
      - relationship:      the relationship type, e.g. "hermana de"

    This always encodes the full phrase so the model always sees
    "María → hermana de → Juan", never an ambiguous "hermana".
    """
    related_character: str
    relationship: str

    def to_dict(self) -> dict:
        return {"related_character": self.related_character, "relationship": self.relationship}

    @classmethod
    def from_dict(cls, d: dict) -> "CharacterRelationship":
        return cls(
            related_character=d.get("related_character", ""),
            relationship=d.get("relationship", ""),
        )

    def to_prompt_line(self) -> str:
        """Return 'relationship related_character', e.g. 'hermana de Juan'."""
        return f"{self.relationship} {self.related_character}"


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
    relationships: list[CharacterRelationship] = field(default_factory=list)

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
            "relationships": [r.to_dict() for r in self.relationships],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Character":
        raw_rels = d.get("relationships", [])
        relationships = []
        for r in raw_rels:
            if isinstance(r, dict):
                rel = CharacterRelationship.from_dict(r)
                # Only keep relationships that have both required fields non-empty.
                if rel.related_character and rel.relationship:
                    relationships.append(rel)
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
            relationships=relationships,
        )

    def to_markdown(self) -> str:
        lines = [f"## {self.name}", f"**Role:** {self.role}", "", f"**Description:** {self.description}", ""]
        if self.backstory:
            lines += [f"**Backstory:** {self.backstory}", ""]
        if self.traits:
            lines += ["**Traits:"]
            lines += [f"- {t}" for t in self.traits]
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
        return {"id": self.id, "number": self.number, "title": self.title, "summary": self.summary, "content": self.content, "reviewed": self.reviewed, "last_review": self.last_review}

    @classmethod
    def from_dict(cls, d: dict) -> "Chapter":
        return cls(id=d.get("id", str(uuid.uuid4())), number=d["number"], title=d.get("title", f"Chapter {d['number']}"), summary=d.get("summary", ""), content=d.get("content", ""), reviewed=d.get("reviewed", False), last_review=d.get("last_review", ""))


@dataclass
class ModelAssignment:
    write_synopsis: str = ""
    generate_outline: str = ""
    review_outline: str = ""
    generate_world: str = ""
    write_chapter: str = ""
    review_chapter: str = ""
    rewrite_chapter: str = ""
    change_chapter: str = ""
    update_memory: str = ""
    conversation_summary: str = ""
    chat: str = ""

    def get(self, task: TaskType) -> str:
        if task == TaskType.WRITE_BOOK:
            return self.write_chapter
        return getattr(self, task.value, "")

    def set(self, task: TaskType, model_path: str) -> None:
        if task == TaskType.WRITE_BOOK:
            self.write_chapter = model_path
        else:
            setattr(self, task.value, model_path)

    def to_dict(self) -> dict:
        return {"write_synopsis": self.write_synopsis, "generate_outline": self.generate_outline, "review_outline": self.review_outline, "generate_world": self.generate_world, "write_chapter": self.write_chapter, "review_chapter": self.review_chapter, "rewrite_chapter": self.rewrite_chapter, "change_chapter": self.change_chapter, "update_memory": self.update_memory, "conversation_summary": self.conversation_summary, "chat": self.chat}

    @classmethod
    def from_dict(cls, d: dict) -> "ModelAssignment":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class ImageModelAssignment:
    default: str = ""
    character_portrait: str = ""
    book_cover: str = ""
    scene_illustration: str = ""
    location: str = ""
    object_item: str = ""

    def get(self, task: "ImageTaskType") -> str:
        specific = getattr(self, task.value, "")
        return specific or self.default

    def set(self, task: "ImageTaskType", model_path: str) -> None:
        setattr(self, task.value, model_path)

    def to_dict(self) -> dict:
        return {"default": self.default, "character_portrait": self.character_portrait, "book_cover": self.book_cover, "scene_illustration": self.scene_illustration, "location": self.location, "object_item": self.object_item}

    @classmethod
    def from_dict(cls, d: dict) -> "ImageModelAssignment":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class TaskTemperatures:
    """
    Per-task generation settings — the single source of truth for
    Temperature, Top P and Top K, one field-set per task, all persisted
    together under the project's existing "task_temperatures" key.

    Top P / Top K are added as sibling per-task fields (mirroring the
    existing temperature fields) rather than as a parallel structure —
    there is no global_top_p / model_top_p, just this one dataclass.
    from_dict() already only accepts known dataclass fields, so projects
    saved before Top P/Top K existed simply fall back to the defaults
    below for those new fields; nothing about temperature or old
    projects/models changes.
    """

    write_synopsis: float = 0.7
    generate_outline: float = 0.7
    review_outline: float = 0.7
    generate_world: float = 0.7
    write_chapter: float = 0.8
    review_chapter: float = 0.7
    rewrite_chapter: float = 0.7
    change_chapter: float = 0.8
    update_memory: float = 0.7
    conversation_summary: float = 0.3
    chat: float = 0.7

    # Top P — defaults match llama-cpp-python's own generate() default (0.9).
    write_synopsis_top_p: float = 0.9
    generate_outline_top_p: float = 0.9
    review_outline_top_p: float = 0.9
    generate_world_top_p: float = 0.9
    write_chapter_top_p: float = 0.9
    review_chapter_top_p: float = 0.9
    rewrite_chapter_top_p: float = 0.9
    change_chapter_top_p: float = 0.9
    update_memory_top_p: float = 0.9
    conversation_summary_top_p: float = 0.9
    chat_top_p: float = 0.9

    # Top K — 40 is llama.cpp's own conventional default; 0 means "disabled"
    # (no top-k filtering), which is respected end-to-end, not just in the UI.
    write_synopsis_top_k: int = 40
    generate_outline_top_k: int = 40
    review_outline_top_k: int = 40
    generate_world_top_k: int = 40
    write_chapter_top_k: int = 40
    review_chapter_top_k: int = 40
    rewrite_chapter_top_k: int = 40
    change_chapter_top_k: int = 40
    update_memory_top_k: int = 40
    conversation_summary_top_k: int = 40
    chat_top_k: int = 40

    def get(self, task: TaskType) -> float:
        if task == TaskType.WRITE_BOOK:
            return self.write_chapter
        return getattr(self, task.value, 0.7)

    def set(self, task: TaskType, temperature: float) -> None:
        if task == TaskType.WRITE_BOOK:
            self.write_chapter = temperature
        else:
            setattr(self, task.value, temperature)

    def get_top_p(self, task: TaskType) -> float:
        if task == TaskType.WRITE_BOOK:
            return self.write_chapter_top_p
        return getattr(self, f"{task.value}_top_p", 0.9)

    def set_top_p(self, task: TaskType, top_p: float) -> None:
        if task == TaskType.WRITE_BOOK:
            self.write_chapter_top_p = top_p
        else:
            setattr(self, f"{task.value}_top_p", top_p)

    def get_top_k(self, task: TaskType) -> int:
        if task == TaskType.WRITE_BOOK:
            return self.write_chapter_top_k
        return getattr(self, f"{task.value}_top_k", 40)

    def set_top_k(self, task: TaskType, top_k: int) -> None:
        if task == TaskType.WRITE_BOOK:
            self.write_chapter_top_k = top_k
        else:
            setattr(self, f"{task.value}_top_k", top_k)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TaskTemperatures":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class AuthorIntent:
    emotional_journey: str = ""
    lasting_impression: str = ""
    themes: str = ""
    unique_elements: str = ""
    inspirations: str = ""
    avoid: str = ""

    def is_empty(self) -> bool:
        return not any([self.emotional_journey, self.lasting_impression, self.themes, self.unique_elements, self.inspirations, self.avoid])

    def to_prompt_fragment(self) -> str:
        if self.is_empty():
            return ""
        lines = []
        if self.emotional_journey: lines.append(f"Emotional journey for the reader: {self.emotional_journey}")
        if self.lasting_impression: lines.append(f"What the reader should remember after finishing: {self.lasting_impression}")
        if self.themes: lines.append(f"Core themes to explore: {self.themes}")
        if self.unique_elements: lines.append(f"What makes this book unique: {self.unique_elements}")
        if self.inspirations: lines.append(f"Inspirations (and what aspect): {self.inspirations}")
        if self.avoid: lines.append(f"Avoid entirely: {self.avoid}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {"emotional_journey": self.emotional_journey, "lasting_impression": self.lasting_impression, "themes": self.themes, "unique_elements": self.unique_elements, "inspirations": self.inspirations, "avoid": self.avoid}

    @classmethod
    def from_dict(cls, d: dict) -> "AuthorIntent":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class WritingStyle:
    narrator_pov: str = ""
    pacing: str = ""
    description_density: str = ""
    dialogue_style: str = ""
    violence_level: str = ""
    romance_level: str = ""
    target_chapter_length: str = ""
    genre_tags: str = ""

    def is_empty(self) -> bool:
        return not any([self.narrator_pov, self.pacing, self.description_density, self.dialogue_style, self.violence_level, self.romance_level, self.target_chapter_length, self.genre_tags])

    def to_prompt_fragment(self) -> str:
        if self.is_empty():
            return ""
        lines = []
        if self.genre_tags: lines.append(f"Genre: {self.genre_tags}.")
        if self.narrator_pov: lines.append(f"Narrative point of view: {self.narrator_pov}.")
        if self.pacing: lines.append(f"Overall pacing: {self.pacing}.")
        if self.description_density: lines.append(f"Description density: {self.description_density}.")
        if self.dialogue_style: lines.append(f"Dialogue style: {self.dialogue_style}.")
        if self.violence_level: lines.append(f"Violence: {self.violence_level}.")
        if self.romance_level: lines.append(f"Romance: {self.romance_level}.")
        if self.target_chapter_length: lines.append(f"Target chapter length: {self.target_chapter_length}.")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {"narrator_pov": self.narrator_pov, "pacing": self.pacing, "description_density": self.description_density, "dialogue_style": self.dialogue_style, "violence_level": self.violence_level, "romance_level": self.romance_level, "target_chapter_length": self.target_chapter_length, "genre_tags": self.genre_tags}

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
    author_intent: AuthorIntent = field(default_factory=AuthorIntent)
    writing_style: WritingStyle = field(default_factory=WritingStyle)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    workflow_status: WorkflowStatus = WorkflowStatus.IDLE
    current_chapter: int = 0
    recent_message_window: int = 20

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "synopsis": self.synopsis, "outline": self.outline, "world": self.world, "memory": self.memory, "chat_summary": self.chat_summary, "model_assignments": self.model_assignments.to_dict(), "task_temperatures": self.task_temperatures.to_dict(), "author_intent": self.author_intent.to_dict(), "writing_style": self.writing_style.to_dict(), "created_at": self.created_at, "updated_at": self.updated_at, "workflow_status": self.workflow_status.value, "current_chapter": self.current_chapter, "recent_message_window": self.recent_message_window}

    @classmethod
    def from_dict(cls, d: dict) -> "Project":
        ma = ModelAssignment.from_dict(d.get("model_assignments", {}))
        tt = TaskTemperatures.from_dict(d.get("task_temperatures", {}))
        ai = AuthorIntent.from_dict(d.get("author_intent", {}))
        ws = WritingStyle.from_dict(d.get("writing_style", {}))
        return cls(id=d.get("id", str(uuid.uuid4())), title=d["title"], synopsis=d.get("synopsis", ""), outline=d.get("outline", ""), world=d.get("world", ""), memory=d.get("memory", ""), chat_summary=d.get("chat_summary", ""), model_assignments=ma, task_temperatures=tt, author_intent=ai, writing_style=ws, created_at=d.get("created_at", datetime.now().isoformat()), updated_at=d.get("updated_at", datetime.now().isoformat()), workflow_status=WorkflowStatus(d.get("workflow_status", "idle")), current_chapter=d.get("current_chapter", 0), recent_message_window=d.get("recent_message_window", 20))


@dataclass
class AppSettings:
    models_directory: str = ""
    default_context_size: int = 4096
    default_gpu_layers: int = 0
    default_threads: int = 4
    default_threads_batch: int = 0
    theme: str = "dark"
    font_size: int = 13
    auto_save: bool = True
    custom_system_prompt: str = ""
    temperature: float = 0.7
    response_language: str = "Español"
    enable_thinking: bool = False
    content_max_tokens: int = 4000
    allow_nsfw: bool = False
    moe_n_batch: int = 1024
    moe_n_ubatch: int = 1024
    log_full_prompts: bool = False
    image_model_path: str = ""
    image_text_encoder_path: str = ""
    image_vae_path: str = ""
    image_backend: str = ImageBackend.STABLE_DIFFUSION_CPP.value
    image_output_directory: str = ""
    image_default_width: int = 512
    image_default_height: int = 512
    image_default_steps: int = 20
    image_default_cfg_scale: float = 7.0
    image_loras: list = None

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
