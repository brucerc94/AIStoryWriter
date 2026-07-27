"""
Story Workflow Engine.

Orchestrates the full novel-writing pipeline:
  Synopsis → Outline → Review → Chapters → Review → Memory → repeat

All heavy work runs in a background QThread so the UI stays responsive.
"""

from __future__ import annotations

import json
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
    Character,
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

    def _task_temperature(self, task: TaskType) -> float:
        """Per-task temperature, set from the Models tab next to that task's model."""
        return self.project.task_temperatures.get(task)

    def _custom_system_instructions(self) -> str:
        """Author-provided instructions from Settings, appended to every prompt."""
        if self.settings is not None:
            return getattr(self.settings, "custom_system_prompt", "") or ""
        return ""

    def _extract_and_merge_characters(self, source_text: str) -> None:
        """
        After the model writes a synopsis/outline/chapter, quietly ask it to
        pull out any named characters mentioned and add genuinely new ones
        to project.characters — otherwise characters the AI introduces only
        ever live inside prose text and never show up in the Characters tab.

        No chat bubble for this (keeps the chat transcript clean), but it
        does emit step_started so the status bar shows *something* is still
        happening — otherwise the UI looks finished-but-frozen for however
        long this extra inference call takes.
        """
        if not source_text or not source_text.strip():
            return
        if not self._load_model_for_task(TaskType.UPDATE_MEMORY):
            logger.warning("Skipping character extraction: no model assigned for Update Memory.")
            return

        self.step_started.emit("Checking for new characters to track...")
        existing_names = ", ".join(c.name for c in self.project.characters) or "(none yet)"
        prompt = (
            "Extract every named character mentioned in the text below. "
            "Respond with ONLY a JSON array — no markdown fences, no commentary, "
            "nothing before or after it. If there are no named characters, "
            "respond with exactly: []\n\n"
            "Each array element must be an object with exactly these keys:\n"
            '  "name": the character\'s name\n'
            '  "role": one of "protagonist", "antagonist", "supporting", "minor"\n'
            '  "description": one concise sentence\n\n'
            f"Characters already tracked (skip these unless the text reveals "
            f"something significant enough to be worth its own new entry): {existing_names}\n\n"
            f"Text:\n{source_text[:6000]}\n\n"
            "JSON array:"
        )
        messages = [
            {
                "role": "system",
                "content": "You extract structured character data as strict JSON. Output JSON only.",
            },
            {"role": "user", "content": prompt},
        ]

        engine = get_engine()
        try:
            raw = engine.generate(
                messages=messages,
                max_tokens=800,
                temperature=0.2,
                stream=True,
                stream_callback=lambda _t: None,  # silent — no chat UI noise
                cancel_check=lambda: self._cancelled,
            )
        except Exception as e:
            logger.warning(f"Character extraction failed (non-fatal): {e}")
            return

        added = self._merge_extracted_characters(raw)
        if added:
            logger.info(f"Auto-added {added} new character(s) from generated text.")

    def _merge_extracted_characters(self, raw_json: str) -> int:
        text = raw_json.strip()
        # Models sometimes wrap JSON in ```json fences despite instructions
        # not to — strip that before parsing.
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
            text = text.strip()

        try:
            data = json.loads(text)
        except Exception:
            logger.warning(
                f"Character extraction: model didn't return valid JSON, skipping. "
                f"Raw (truncated): {text[:200]!r}"
            )
            return 0

        if not isinstance(data, list):
            return 0

        existing_lower = {c.name.strip().lower() for c in self.project.characters}
        added = 0
        for entry in data:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name", "")).strip()
            if not name or name.lower() in existing_lower:
                continue
            role = str(entry.get("role", "supporting")).strip().lower()
            if role not in ("protagonist", "antagonist", "supporting", "minor"):
                role = "supporting"
            description = str(entry.get("description", "")).strip()
            self.project.characters.append(
                Character(name=name, role=role, description=description)
            )
            existing_lower.add(name.lower())
            added += 1
        return added

    def _extract_and_merge_world_info(self, source_text: str) -> None:
        """
        After the model writes a synopsis/outline/chapter, quietly ask it to
        pull out any NEW worldbuilding details (locations, rules/systems,
        history, culture, technology) not already covered in project.world,
        and append them — mirroring how Story Memory already accumulates,
        rather than trying to structurally dedupe free-form world text.

        No chat bubble, but does emit step_started for status-bar feedback.
        """
        if not source_text or not source_text.strip():
            return
        if not self._load_model_for_task(TaskType.UPDATE_MEMORY):
            logger.warning("Skipping world-info extraction: no model assigned for Update Memory.")
            return

        self.step_started.emit("Checking for new world details...")
        existing_world = self.project.world.strip() or "(nothing recorded yet)"
        prompt = (
            "Below are the story's existing world-building notes, followed by "
            "newly written story text. Extract ONLY genuinely NEW world-building "
            "details from the new text — locations, rules/systems, history, "
            "culture, technology — that are NOT already covered in the existing "
            "notes. Do not repeat anything already listed. Format as a short "
            "Markdown bullet list. If there is nothing new, respond with "
            "exactly: NO_NEW_WORLD_DETAILS\n\n"
            f"## Existing World Notes\n{existing_world}\n\n"
            f"## New Text\n{source_text[:6000]}\n\n"
            "New details (bullet list, or NO_NEW_WORLD_DETAILS):"
        )
        messages = [
            {
                "role": "system",
                "content": "You extract new worldbuilding details as a concise Markdown bullet list.",
            },
            {"role": "user", "content": prompt},
        ]

        engine = get_engine()
        try:
            raw = engine.generate(
                messages=messages,
                max_tokens=600,
                temperature=0.3,
                stream=True,
                stream_callback=lambda _t: None,  # silent — no chat UI noise
                cancel_check=lambda: self._cancelled,
            )
        except Exception as e:
            logger.warning(f"World-info extraction failed (non-fatal): {e}")
            return

        cleaned = raw.strip()
        if not cleaned or "NO_NEW_WORLD_DETAILS" in cleaned.upper():
            return

        sep = "\n\n" if self.project.world.strip() else ""
        self.project.world = (self.project.world.rstrip() + sep + cleaned).strip()
        logger.info("Added new world-building details extracted from generated text.")

    def _run_inference(
        self,
        task: TaskType,
        user_message: str,
        add_to_chat: bool = True,
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

        temperature = self._task_temperature(task)
        logger.info(
            f"[{task.value}] running inference: {len(messages)} messages, "
            f"temperature={temperature}, max_tokens={max_tokens}"
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
                temperature=temperature,
                stream=True,
                stream_callback=on_token,
                cancel_check=lambda: self._cancelled,
            )
        except Exception as e:
            logger.error(f"[{task.value}] inference error: {e}\n{traceback.format_exc()}")
            self.error_occurred.emit(f"Inference error: {e}\n{traceback.format_exc()}")
            return ""

        if self._cancelled:
            logger.info(f"[{task.value}] generation stopped by user "
                        f"({len(result)} chars generated before stop).")

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
        elif task == TaskType.GENERATE_WORLD:
            self._run_generate_world()
        elif task == TaskType.WRITE_CHAPTER:
            self._run_write_chapter()
        elif task == TaskType.REVIEW_CHAPTER:
            self._run_review_chapter()
        elif task == TaskType.REWRITE_CHAPTER:
            self._run_rewrite_chapter()
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
            self._extract_and_merge_characters(result)
            self._extract_and_merge_world_info(result)
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
            self._extract_and_merge_characters(result)
            self._extract_and_merge_world_info(result)
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

    def _run_generate_world(self) -> None:
        self.step_started.emit("Generating world & setting...")
        prompt = (
            self.extra_input
            if self.extra_input
            else (
                f"Write detailed worldbuilding notes for '{self.project.title}', "
                "covering geography, history, rules/systems, culture, and "
                "technology as relevant to the story."
            )
        )
        result = self._run_inference(
            TaskType.GENERATE_WORLD, prompt, add_to_chat=True, max_tokens=2000
        )
        if result:
            # Append rather than overwrite — world notes accumulate over time,
            # same as Story Memory, instead of replacing manual notes the
            # author already wrote.
            sep = "\n\n---\n\n" if self.project.world.strip() else ""
            self.project.world = (self.project.world.rstrip() + sep + result).strip()
            self._extract_and_merge_characters(result)
            storage.save_project(self.project)
            self.step_finished.emit("World & Setting", result)

    def _run_write_chapter(self) -> None:
        # Never trust project.current_chapter alone — if it's stale (e.g.
        # left over from before a chapter was manually deleted, or set
        # incorrectly by some other UI action) always continue forward
        # from whichever chapter number is actually highest on disk.
        highest_existing = max(
            (c.number for c in self.project.chapters), default=0
        )
        chapter_num = max(self.project.current_chapter, highest_existing) + 1
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
            TaskType.WRITE_CHAPTER, prompt, add_to_chat=True, max_tokens=4000
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

            self._extract_and_merge_characters(result)
            self._extract_and_merge_world_info(result)
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
            chapter.reviewed = True
            chapter.last_review = result  # keep it so "Rewrite with Feedback" can use it
            storage.save_project(self.project)
            self.step_finished.emit(f"Review of Chapter {chapter_num}", result)

    def _run_rewrite_chapter(self) -> None:
        chapter_num = self.project.current_chapter
        if chapter_num == 0:
            chapter_num = len(self.project.chapters)

        chapter = next((c for c in self.project.chapters if c.number == chapter_num), None)
        if not chapter:
            self.error_occurred.emit(f"Chapter {chapter_num} not found.")
            return

        if not chapter.last_review.strip():
            self.error_occurred.emit(
                f"No review feedback saved for Chapter {chapter_num} yet. "
                "Click \"Review\" first, then \"Rewrite with Feedback\"."
            )
            return

        self.step_started.emit(f"Rewriting Chapter {chapter_num} with review feedback...")

        prompt = (
            self.extra_input
            if self.extra_input
            else (
                f"Chapter {chapter_num}: '{chapter.title}'\n\n"
                f"Current content:\n{chapter.content}\n\n"
                f"Review feedback to address:\n{chapter.last_review}"
            )
        )
        result = self._run_inference(
            TaskType.REWRITE_CHAPTER, prompt, add_to_chat=True, max_tokens=4000
        )
        if result:
            chapter.content = result
            chapter.reviewed = False  # it changed — worth another look before moving on
            chapter.last_review = ""
            self._extract_and_merge_characters(result)
            storage.save_project(self.project)
            self.step_finished.emit(f"Rewrite of Chapter {chapter_num}", result)

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
                temperature=self._task_temperature(TaskType.CONVERSATION_SUMMARY),
                stream=True,
                stream_callback=on_token,
                cancel_check=lambda: self._cancelled,
            )
        except Exception as e:
            logger.error(f"[conversation_summary] error: {e}")
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
