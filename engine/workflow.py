"""
Story Workflow Engine.

Orchestrates the full novel-writing pipeline:
  Synopsis → Outline → Review → Chapters → Review → Memory → repeat

All heavy work runs in a background QThread so the UI stays responsive.
"""

from __future__ import annotations

import logging
import traceback
from typing import Callable, Optional

from PySide6.QtCore import QObject, QThread, Signal

from engine.chat import get_engine
from engine.context import (
    build_context_for_model,
    build_system_prompt,
    build_summarization_prompt,
    mark_old_messages_summarized,
    should_summarize,
)
from engine.models import (
    Chapter,
    ChatMessage,
    MessageRole,
    Project,
    TaskType,
    WorkflowStatus,
)
from engine import storage

logger = logging.getLogger("workflow")


class WorkflowWorker(QObject):
    """Runs inside a QThread. Emits signals back to the UI."""

    token_received = Signal(str)          # streaming token
    step_started = Signal(str)            # step description
    step_finished = Signal(str, str)      # step description, full result
    error_occurred = Signal(str)          # error message
    finished = Signal()                   # all done
    model_loading = Signal(str)           # model load status
    approval_needed = Signal(str, str)    # step_name, content — UI must approve

    def __init__(
        self,
        project: Project,
        task: TaskType,
        extra_input: str = "",
        settings=None,
    ) -> None:
        super().__init__()
        self.project = project
        self.task = task
        self.extra_input = extra_input
        self.settings = settings
        self._cancelled = False
        self._approval_result: Optional[bool] = None
        self._approval_event = __import__("threading").Event()

    def cancel(self) -> None:
        self._cancelled = True

    def approve(self, approved: bool) -> None:
        self._approval_result = approved
        self._approval_event.set()

    def _wait_for_approval(self) -> bool:
        self._approval_event.wait()
        self._approval_event.clear()
        return bool(self._approval_result)

    def _load_model_for_task(self, task: TaskType) -> bool:
        model_path = self.project.model_assignments.get(task)
        if not model_path:
            # Fall back to chat model
            model_path = self.project.model_assignments.get(TaskType.CHAT)
        if not model_path:
            logger.error(f"No model assigned for task '{task.value}' (and no chat fallback).")
            self.error_occurred.emit(
                f"No model assigned for task '{task.value}'. "
                "Please assign a model in the Models tab."
            )
            return False

        engine = get_engine()
        if not engine.is_available:
            logger.error("llama-cpp-python is not installed.")
            self.error_occurred.emit(
                "llama-cpp-python is not installed.\n"
                "Run: pip install llama-cpp-python"
            )
            return False

        ctx = 4096
        gpu = 0
        threads = 4
        if self.settings:
            ctx = self.settings.default_context_size
            gpu = self.settings.default_gpu_layers
            threads = self.settings.default_threads

        logger.info(
            f"[{task.value}] requesting model load: {model_path} "
            f"(n_ctx={ctx}, n_gpu_layers={gpu}, n_threads={threads})"
        )
        try:
            engine.load_model(
                model_path,
                n_ctx=ctx,
                n_gpu_layers=gpu,
                n_threads=threads,
                progress_callback=lambda msg: self.model_loading.emit(msg),
            )
            return True
        except Exception as e:
            logger.error(f"Failed to load model '{model_path}': {e}")
            self.error_occurred.emit(f"Failed to load model: {e}")
            return False

    def _effective_temperature(self, task_default: float) -> float:
        """
        Settings.temperature (from the Settings tab) overrides every
        per-task default so the author has direct, explicit control.
        Falls back to the task's own default if no settings are attached.
        """
        if self.settings is not None:
            return getattr(self.settings, "temperature", task_default)
        return task_default

    def _custom_system_instructions(self) -> str:
        """Author-provided instructions from Settings, appended to every prompt."""
        if self.settings is not None:
            return getattr(self.settings, "custom_system_prompt", "") or ""
        return ""

    def _run_inference(
        self,
        task: TaskType,
        user_message: str,
        add_to_chat: bool = True,
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> str:
        """Load model, build context, run inference, optionally store in chat."""
        if self._cancelled:
            logger.info(f"[{task.value}] cancelled before inference started.")
            return ""

        if not self._load_model_for_task(task):
            return ""

        system_prompt = build_system_prompt(
            self.project, task, custom_instructions=self._custom_system_instructions()
        )
        messages = build_context_for_model(
            self.project,
            user_message,
            system_prompt,
            max_context_tokens=3200,
        )

        effective_temp = self._effective_temperature(temperature)
        logger.info(
            f"[{task.value}] running inference: {len(messages)} messages, "
            f"temperature={effective_temp}, max_tokens={max_tokens}"
        )

        accumulated = []

        def on_token(token: str) -> None:
            accumulated.append(token)
            self.token_received.emit(token)

        engine = get_engine()
        try:
            result = engine.generate(
                messages=messages,
                max_tokens=max_tokens,
                temperature=effective_temp,
                stream=True,
                stream_callback=on_token,
            )
        except Exception as e:
            logger.error(f"[{task.value}] inference error: {e}\n{traceback.format_exc()}")
            self.error_occurred.emit(f"Inference error: {e}\n{traceback.format_exc()}")
            return ""

        logger.info(f"[{task.value}] inference complete: {len(result)} chars generated.")

        if add_to_chat:
            # Record user message and assistant response in chat history
            user_msg = ChatMessage(role=MessageRole.USER, content=user_message)
            assistant_msg = ChatMessage(role=MessageRole.ASSISTANT, content=result)
            self.project.chat_messages.append(user_msg)
            self.project.chat_messages.append(assistant_msg)

        return result

    def run(self) -> None:
        logger.info(f"[workflow] Task started: {self.task.value} (project='{self.project.title}')")
        try:
            self._dispatch()
            logger.info(f"[workflow] Task finished: {self.task.value}")
        except Exception as e:
            logger.error(f"[workflow] Task error: {self.task.value}: {e}\n{traceback.format_exc()}")
            self.error_occurred.emit(f"Workflow error: {e}\n{traceback.format_exc()}")
        finally:
            self.finished.emit()

    def _dispatch(self) -> None:
        task = self.task
        if task == TaskType.CHAT:
            self._run_chat()
        elif task == TaskType.WRITE_SYNOPSIS:
            self._run_write_synopsis()
        elif task == TaskType.GENERATE_OUTLINE:
            self._run_generate_outline()
        elif task == TaskType.REVIEW_OUTLINE:
            self._run_review_outline()
        elif task == TaskType.WRITE_CHAPTER:
            self._run_write_chapter()
        elif task == TaskType.REVIEW_CHAPTER:
            self._run_review_chapter()
        elif task == TaskType.UPDATE_MEMORY:
            self._run_update_memory()
        elif task == TaskType.CONVERSATION_SUMMARY:
            self._run_conversation_summary()

    # ──────────────────────────────────────────────
    # Task implementations
    # ──────────────────────────────────────────────

    def _run_chat(self) -> None:
        self.step_started.emit("Generating response...")
        result = self._run_inference(TaskType.CHAT, self.extra_input, add_to_chat=True)
        if result:
            self._maybe_summarize()
            storage.save_project(self.project)
            self.step_finished.emit("Chat", result)

    def _run_write_synopsis(self) -> None:
        self.step_started.emit("Writing synopsis...")
        prompt = (
            self.extra_input
            if self.extra_input
            else f"Write a synopsis for a novel titled '{self.project.title}'."
        )
        result = self._run_inference(TaskType.WRITE_SYNOPSIS, prompt, add_to_chat=True, max_tokens=1024)
        if result:
            self.project.synopsis = result
            storage.save_project(self.project)
            self.step_finished.emit("Synopsis", result)

    def _run_generate_outline(self) -> None:
        self.step_started.emit("Generating outline...")
        prompt = (
            self.extra_input
            if self.extra_input
            else (
                f"Generate a complete chapter-by-chapter outline for '{self.project.title}'.\n"
                + (f"Synopsis: {self.project.synopsis}" if self.project.synopsis else "")
            )
        )
        result = self._run_inference(
            TaskType.GENERATE_OUTLINE, prompt, add_to_chat=True, max_tokens=3000
        )
        if result:
            self.project.outline = result
            storage.save_project(self.project)
            self.step_finished.emit("Outline", result)

    def _run_review_outline(self) -> None:
        self.step_started.emit("Reviewing outline...")
        if not self.project.outline:
            self.error_occurred.emit("No outline to review. Generate an outline first.")
            return
        prompt = f"Please review this outline:\n\n{self.project.outline}"
        result = self._run_inference(
            TaskType.REVIEW_OUTLINE, prompt, add_to_chat=True, max_tokens=2048
        )
        if result:
            storage.save_project(self.project)
            self.step_finished.emit("Outline Review", result)

    def _run_write_chapter(self) -> None:
        chapter_num = self.project.current_chapter + 1
        self.step_started.emit(f"Writing Chapter {chapter_num}...")

        # Find chapter summary from outline if available
        chapter_context = ""
        if self.project.outline:
            chapter_context = f"\nOutline:\n{self.project.outline}"

        if self.extra_input:
            prompt = self.extra_input
        else:
            prompt = (
                f"Write Chapter {chapter_num} of '{self.project.title}'."
                f"{chapter_context}"
            )

        result = self._run_inference(
            TaskType.WRITE_CHAPTER, prompt, add_to_chat=True, max_tokens=4000, temperature=0.8
        )
        if result:
            # Save chapter
            existing = next((c for c in self.project.chapters if c.number == chapter_num), None)
            if existing:
                existing.content = result
                existing.reviewed = False
            else:
                ch = Chapter(
                    number=chapter_num,
                    title=f"Chapter {chapter_num}",
                    content=result,
                )
                self.project.chapters.append(ch)

            storage.save_project(self.project)
            self.step_finished.emit(f"Chapter {chapter_num}", result)

    def _run_review_chapter(self) -> None:
        chapter_num = self.project.current_chapter
        if chapter_num == 0:
            chapter_num = len(self.project.chapters)

        self.step_started.emit(f"Reviewing Chapter {chapter_num}...")

        chapter = next((c for c in self.project.chapters if c.number == chapter_num), None)
        if not chapter:
            self.error_occurred.emit(f"Chapter {chapter_num} not found.")
            return

        prompt = f"Review Chapter {chapter_num}: '{chapter.title}'\n\n{chapter.content}"
        result = self._run_inference(
            TaskType.REVIEW_CHAPTER, prompt, add_to_chat=True, max_tokens=2048
        )
        if result:
            if chapter:
                chapter.reviewed = True
            storage.save_project(self.project)
            self.step_finished.emit(f"Review of Chapter {chapter_num}", result)

    def _run_update_memory(self) -> None:
        self.step_started.emit("Updating story memory...")

        chapter_num = self.project.current_chapter
        if chapter_num == 0:
            chapter_num = len(self.project.chapters)

        chapter = next((c for c in self.project.chapters if c.number == chapter_num), None)
        if not chapter:
            self.error_occurred.emit("No chapter found to extract memory from.")
            return

        existing = f"\n\nExisting memory:\n{self.project.memory}" if self.project.memory else ""
        prompt = (
            f"Update the story memory after Chapter {chapter_num}: '{chapter.title}'.\n\n"
            f"Chapter content:\n{chapter.content}"
            f"{existing}"
        )

        result = self._run_inference(
            TaskType.UPDATE_MEMORY, prompt, add_to_chat=False, max_tokens=2048
        )
        if result:
            self.project.memory = result
            self.project.current_chapter = chapter_num
            storage.save_project(self.project)
            self.step_finished.emit("Story Memory", result)

    def _run_conversation_summary(self) -> None:
        self.step_started.emit("Summarizing conversation history...")

        to_summarize = mark_old_messages_summarized(self.project)
        if not to_summarize:
            self.step_finished.emit("Summary", "Nothing to summarize.")
            return

        prompt = build_summarization_prompt(to_summarize)

        if not self._load_model_for_task(TaskType.CONVERSATION_SUMMARY):
            return

        system = "You are a helpful assistant that summarizes conversations."
        custom_instructions = self._custom_system_instructions()
        if custom_instructions:
            system += f"\n\n## Additional Author Instructions\n{custom_instructions}"

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]

        accumulated = []

        def on_token(t: str) -> None:
            accumulated.append(t)
            self.token_received.emit(t)

        engine = get_engine()
        try:
            result = engine.generate(
                messages=messages,
                max_tokens=1024,
                temperature=self._effective_temperature(0.3),
                stream=True,
                stream_callback=on_token,
            )
        except Exception as e:
            self.error_occurred.emit(f"Summary error: {e}")
            return

        # Append to existing summary
        sep = "\n\n---\n\n" if self.project.chat_summary else ""
        self.project.chat_summary = self.project.chat_summary + sep + result
        storage.save_project(self.project)
        self.step_finished.emit("Conversation Summary", result)

    def _maybe_summarize(self) -> None:
        """Auto-trigger summarization if needed."""
        if should_summarize(self.project):
            self._run_conversation_summary()


class WorkflowThread(QThread):
    """Owns the worker and runs it in a background thread."""

    token_received = Signal(str)
    step_started = Signal(str)
    step_finished = Signal(str, str)
    error_occurred = Signal(str)
    model_loading = Signal(str)
    approval_needed = Signal(str, str)

    def __init__(
        self,
        project: Project,
        task: TaskType,
        extra_input: str = "",
        settings=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.worker = WorkflowWorker(project, task, extra_input, settings)
        self.worker.moveToThread(self)

        # Forward signals
        self.worker.token_received.connect(self.token_received)
        self.worker.step_started.connect(self.step_started)
        self.worker.step_finished.connect(self.step_finished)
        self.worker.error_occurred.connect(self.error_occurred)
        self.worker.model_loading.connect(self.model_loading)
        self.worker.approval_needed.connect(self.approval_needed)

    def run(self) -> None:
        self.worker.run()

    def cancel(self) -> None:
        self.worker.cancel()
