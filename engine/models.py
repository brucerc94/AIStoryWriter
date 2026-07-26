"""
Core data models for AI Story Studio.
All entities are plain dataclasses — no ORM, no database.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class TaskType(str, Enum):
    WRITE_SYNOPSIS = "write_synopsis"
    GENERATE_OUTLINE = "generate_outline"
    REVIEW_OUTLINE = "review_outline"
    GENERATE_WORLD = "generate_world"
    WRITE_CHAPTER = "write_chapter"
    REVIEW_CHAPTER = "review_chapter"
    REWRITE_CHAPTER = "rewrite_chapter"
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

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "description": self.description,
            "backstory": self.backstory,
            "traits": self.traits,
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

    def to_dict(self) -> dict:
        return {
            "models_directory": self.models_directory,
            "default_context_size": self.default_context_size,
            "default_gpu_layers": self.default_gpu_layers,
            "default_threads": self.default_threads,
            "theme": self.theme,
            "font_size": self.font_size,
            "auto_save": self.auto_save,
            "custom_system_prompt": self.custom_system_prompt,
            "temperature": self.temperature,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AppSettings":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
