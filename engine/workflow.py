"""
Story Workflow Engine.

Orchestrates the full novel-writing pipeline:
  Synopsis → Outline → Review → Chapters → Review → Memory → repeat

All heavy work runs in a background QThread so the UI stays responsive.
"""

from __future__ import annotations

import json
import logging
import re
import traceback
import unicodedata
from typing import Callable, Optional

from PySide6.QtCore import QObject, QThread, Signal

from engine.chat import get_engine
from engine.context import (
    build_context_for_model,
    build_review_context_for_model,
    build_system_prompt,
    estimate_messages_tokens,
    estimate_context_usage,
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

MAX_CONTINUATIONS = 3
MAX_COMPLETION_EVAL_RETRIES = 2
# Outline generation can need more passes than a single chapter: a 40-chapter
# request won't fit in one 3000-token pass, so allow more continuation rounds
# than MAX_CONTINUATIONS (which is sized for one chapter's prose).
MAX_OUTLINE_CONTINUATIONS = 8


class WorkflowWorker(QObject):
    """Runs inside a QThread. Emits signals back to the UI."""

    token_received = Signal(str)          # streaming token
    step_started = Signal(str)            # step description
    step_finished = Signal(str, str)      # step description, full result
    error_occurred = Signal(str)          # error message
    finished = Signal()                   # all done
    model_loading = Signal(str)           # model load status
    approval_needed = Signal(str, str)    # step_name, content — UI must approve
    clear_chat_requested = Signal()       # UI should clear temporary chat history

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
        self._stop_after_current_chapter = False
        self._approval_result: Optional[bool] = None
        self._approval_event = __import__("threading").Event()

    def cancel(self) -> None:
        self._cancelled = True

    def request_stop_after_current_chapter(self) -> None:
        self._stop_after_current_chapter = True

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
        threads_batch = 0
        moe_n_batch = 1024
        moe_n_ubatch = 1024
        if self.settings:
            ctx = self.settings.default_context_size
            gpu = self.settings.default_gpu_layers
            threads = self.settings.default_threads
            threads_batch = getattr(self.settings, "default_threads_batch", 0)
            moe_n_batch = getattr(self.settings, "moe_n_batch", 1024)
            moe_n_ubatch = getattr(self.settings, "moe_n_ubatch", 1024)

        logger.info(
            f"[{task.value}] requesting model load: {model_path} "
            f"(n_ctx={ctx}, n_gpu_layers={gpu}, n_threads={threads}, "
            f"n_threads_batch={threads_batch or 'auto'})"
        )
        try:
            engine.load_model(
                model_path,
                n_ctx=ctx,
                n_gpu_layers=gpu,
                n_threads=threads,
                n_threads_batch=threads_batch,
                moe_n_batch=moe_n_batch,
                moe_n_ubatch=moe_n_ubatch,
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

    def _model_context_limit(self) -> int:
        engine = get_engine()
        limit = engine.current_context_size
        if limit <= 0:
            raise RuntimeError("No active model context size available.")
        return limit

    def _build_inference_messages(
        self,
        task: TaskType,
        user_message: str,
        system_prompt: str,
        context_limit: int,
        reply_reserved: int,
    ) -> list[dict]:
        if task == TaskType.REVIEW_CHAPTER:
            return build_review_context_for_model(
                self.project,
                user_message,
                system_prompt,
                max_context_tokens=context_limit,
                reply_reserved=reply_reserved,
            )
        return build_context_for_model(
            self.project,
            user_message,
            system_prompt,
            max_context_tokens=context_limit,
            task=task,
            reply_reserved=reply_reserved,
        )

    def _run_inference_v2(
        self,
        task: TaskType,
        user_message: str,
        add_to_chat: bool = True,
        max_tokens: int = 2048,
    ) -> str:
        if self._cancelled:
            logger.info(f"[{task.value}] cancelled before inference started.")
            return ""

        if not self._load_model_for_task(task):
            return ""

        system_prompt = build_system_prompt(
            self.project,
            task,
            custom_instructions=self._custom_system_instructions(),
            language=self._response_language(),
            allow_nsfw=self._allow_nsfw(),
        )
        context_limit = self._model_context_limit()
        reply_reserved = max(256, min(4096, context_limit // 3))

        candidate_budgets = [context_limit]
        for factor in (0.85, 0.70, 0.55, 0.40):
            reduced = int(context_limit * factor)
            if reduced >= 1024:
                candidate_budgets.append(reduced)
        candidate_budgets.append(1024)

        messages: list[dict] = []
        selected_budget = context_limit
        prompt_tokens = 0
        seen_budgets = set()

        for budget in candidate_budgets:
            if budget in seen_budgets:
                continue
            seen_budgets.add(budget)
            candidate_messages = self._build_inference_messages(
                task, user_message, system_prompt, budget, reply_reserved
            )
            candidate_prompt_tokens = estimate_messages_tokens(candidate_messages)
            candidate_effective_reply = min(max_tokens, max(0, context_limit - candidate_prompt_tokens))
            if candidate_prompt_tokens + candidate_effective_reply <= context_limit:
                messages = candidate_messages
                selected_budget = budget
                prompt_tokens = candidate_prompt_tokens
                break
            if not messages:
                messages = candidate_messages
                selected_budget = budget
                prompt_tokens = candidate_prompt_tokens

        if not messages:
            self.error_occurred.emit("Could not build a valid prompt for inference.")
            return ""

        if task == TaskType.REVIEW_CHAPTER:
            if any(m.get("role") in ("user", "assistant") for m in messages[1:-1]):
                error = "[review_chapter] review context unexpectedly contains chat history."
                logger.error(error)
                self.error_occurred.emit(error)
                return ""

        effective_max_tokens = min(max_tokens, max(0, context_limit - prompt_tokens))
        if prompt_tokens + effective_max_tokens > context_limit:
            error = (
                f"[{task.value}] prompt_tokens({prompt_tokens}) + "
                f"effective_reply({effective_max_tokens}) exceeds model_n_ctx({context_limit})"
            )
            logger.error(error)
            self.error_occurred.emit(error)
            return ""

        temperature = self._task_temperature(task)
        logger.info(
            f"[{task.value}] running inference: {len(messages)} messages, "
            f"temperature={temperature}, max_tokens={effective_max_tokens}"
        )
        logger.info(
            f"[{task.value}] context budget: "
            f"Context Limit={context_limit} "
            f"Prompt Tokens={prompt_tokens} "
            f"Available Reply={context_limit - prompt_tokens} "
            f"Requested Reply={max_tokens} "
            f"Effective Reply={effective_max_tokens}"
        )
        logger.info(
            f"[{task.value}] prompt detail: selected_budget={selected_budget}, "
            f"used={prompt_tokens + effective_max_tokens}, "
            f"remaining≈{context_limit - (prompt_tokens + effective_max_tokens)}"
        )
        if selected_budget != context_limit:
            logger.info(f"[{task.value}] context compacted before inference.")
        if effective_max_tokens < max_tokens:
            logger.warning(
                f"[{task.value}] requested max_tokens={max_tokens} but only "
                f"{effective_max_tokens} reply tokens fit in context. "
                "The response will be capped to the effective limit."
            )

        # ── Full prompt dump ──────────────────────────────────────────────────
        # Printed to stdout so it's always visible in the terminal regardless
        # of the logging level configured elsewhere. Each message role is
        # clearly separated so the structure is easy to scan at a glance.
        _ROLE_COLORS = {
            "system":    "\033[36m",   # cyan
            "user":      "\033[32m",   # green
            "assistant": "\033[33m",   # yellow
        }
        _RESET = "\033[0m"
        _DIVIDER = "─" * 72

        print(f"\n{'═' * 72}")
        print(f"  PROMPT DUMP  [{task.value.upper()}]  "
              f"{len(messages)} msg(s)  ~{prompt_tokens} tokens")
        print(f"{'═' * 72}")
        for idx, msg in enumerate(messages):
            role = msg.get("role", "?")
            content = msg.get("content", "")
            color = _ROLE_COLORS.get(role, "")
            print(f"{color}[{idx}] {role.upper()}{_RESET}")
            print(_DIVIDER)
            print(content)
            print()
        print(f"{'═' * 72}\n")

        accumulated = []

        def on_token(token: str) -> None:
            accumulated.append(token)
            self.token_received.emit(token)

        engine = get_engine()
        try:
            result = engine.generate(
                messages=messages,
                max_tokens=effective_max_tokens,
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
            logger.info(f"[{task.value}] generation stopped by user ({len(result)} chars generated before stop).")

        logger.info(f"[{task.value}] inference complete: {len(result)} chars generated.")

        if add_to_chat:
            self.project.chat_messages.append(ChatMessage(role=MessageRole.USER, content=user_message))
            self.project.chat_messages.append(ChatMessage(role=MessageRole.ASSISTANT, content=result))

        return result

    def _content_max_tokens(self) -> int:
        """
        Shared reply-length budget for the long-form content-writing tasks:
        GENERATE_OUTLINE, WRITE_CHAPTER (and therefore WRITE_BOOK, which
        just calls it per chapter), and REWRITE_CHAPTER. One setting
        (Settings > Generation > Max Tokens per Pass) instead of separate
        hardcoded numbers per task, so raising it for longer chapters also
        raises it for the outline instead of the two silently drifting
        apart. Falls back to 4000 for settings saved before this option
        existed.
        """
        if self.settings is not None:
            value = getattr(self.settings, "content_max_tokens", 4000)
            if isinstance(value, int) and value > 0:
                return value
        return 4000

    def _custom_system_instructions(self) -> str:
        """Author-provided instructions from Settings, appended to every prompt."""
        if self.settings is not None:
            return getattr(self.settings, "custom_system_prompt", "") or ""
        return ""

    def _response_language(self) -> str:
        """Language to write responses in, from Settings."""
        if self.settings is not None:
            return getattr(self.settings, "response_language", "") or ""
        return ""

    def _allow_nsfw(self) -> bool:
        """Whether NSFW content is permitted, from Settings."""
        if self.settings is not None:
            return bool(getattr(self.settings, "allow_nsfw", False))
        return False

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
        language = self._response_language()
        language_note = (
            f' The "description" value should be written in {language}.'
            if language else ""
        )
        # Process text in 6000-char chunks so long outlines (12+ chapters)
        # are fully scanned instead of being silently truncated at 6000 chars.
        CHUNK_SIZE = 6000
        text_chunks = [
            source_text[i:i + CHUNK_SIZE]
            for i in range(0, max(1, len(source_text)), CHUNK_SIZE)
        ]
        total_added = 0
        for chunk_idx, chunk in enumerate(text_chunks):
            if self._cancelled:
                break
            if chunk_idx > 0:
                # Update existing_names so later chunks don't re-add chars
                # already picked up in earlier chunks.
                existing_names = ", ".join(c.name for c in self.project.characters) or "(none yet)"
            prompt = (
                "Extract every named character mentioned in the text below. "
                "Respond with ONLY a JSON array — no markdown fences, no commentary, "
                "nothing before or after it. If there are no named characters, "
                "respond with exactly: []\n\n"
                "Each array element must be an object with exactly these keys "
                "(keep these key names and the role value in English — only the "
                f"description text should be translated):\n"
                '  "name": the character\'s name\n'
                '  "role": one of "protagonist", "antagonist", "supporting", "minor"\n'
                '  "description": one concise sentence describing only visible appearance and physical traits. '
                'It must include age, ethnicity or race, overall appearance, notable physical features, scars or facial traits, and clothing/style if relevant. '
                'Do not include personality, backstory, memory, motivations, secrets, or inner emotions. '
                f"{language_note}\n\n"
                f"Characters already tracked (skip these unless the text reveals "
                f"something significant enough to be worth its own new entry): {existing_names}\n\n"
                f"Text:\n{chunk}\n\n"
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
                logger.warning(f"Character extraction failed (non-fatal) on chunk {chunk_idx + 1}: {e}")
                continue

            added = self._merge_extracted_characters(raw)
            total_added += added
            if added:
                logger.info(f"Auto-added {added} new character(s) from chunk {chunk_idx + 1}/{len(text_chunks)}.")

        if total_added:
            logger.info(f"Character extraction complete: {total_added} total new character(s) added.")

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
        language = self._response_language()
        language_note = f" Write it in {language}." if language else ""
        # Process text in 6000-char chunks so long outlines/chapters don't
        # get silently truncated and miss world details from later sections.
        CHUNK_SIZE = 6000
        text_chunks = [
            source_text[i:i + CHUNK_SIZE]
            for i in range(0, max(1, len(source_text)), CHUNK_SIZE)
        ]
        for chunk_idx, chunk in enumerate(text_chunks):
            if self._cancelled:
                break
            # Always use the latest accumulated world notes as the baseline
            # so each chunk can see what previous chunks already added.
            existing_world = self.project.world.strip() or "(nothing recorded yet)"
            prompt = (
                "Below are the story's existing world-building notes, followed by "
                "newly written story text. Extract ONLY genuinely NEW world-building "
                "details from the new text — locations, rules/systems, history, "
                "culture, technology — that are NOT already covered in the existing "
                "notes. Do not repeat anything already listed. Format as a short "
                f"Markdown bullet list.{language_note} If there is nothing new, "
                "respond with exactly: NO_NEW_WORLD_DETAILS\n\n"
                f"## Existing World Notes\n{existing_world}\n\n"
                f"## New Text\n{chunk}\n\n"
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
                logger.warning(f"World-info extraction failed (non-fatal) on chunk {chunk_idx + 1}: {e}")
                continue

            cleaned = raw.strip()
            if not cleaned or "NO_NEW_WORLD_DETAILS" in cleaned.upper():
                continue

            sep = "\n\n" if self.project.world.strip() else ""
            self.project.world = (self.project.world.rstrip() + sep + cleaned).strip()
            logger.info(f"Added new world-building details from chunk {chunk_idx + 1}/{len(text_chunks)}.")

    def _run_inference(
        self,
        task: TaskType,
        user_message: str,
        add_to_chat: bool = True,
        max_tokens: int = 2048,
    ) -> str:
        return self._run_inference_v2(task, user_message, add_to_chat=add_to_chat, max_tokens=max_tokens)

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
        elif task == TaskType.WRITE_BOOK:
            self._run_write_book()
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

    def _detect_chapter_continuation_request(self, message: str) -> Optional[int]:
        """
        Heuristic: does this chat message look like "continue chapter 3" /
        "sigue con el capítulo 3" / "sigue escribiendo" / "continúa la
        historia"? Returns the target chapter number if so, else None.

        This lets chapters keep growing through ordinary conversation
        instead of being capped at whatever a single Write Chapter call
        produces — "continue" in chat actually appends to the chapter's
        saved content rather than just being a normal chat reply about it.
        """
        if not self.project.chapters:
            return None

        lower = message.lower()
        # Strip accents so "continúa"/"continua", "está"/"esta", etc. all
        # match the same plain-ASCII verb stems below.
        normalized = (
            unicodedata.normalize("NFKD", lower)
            .encode("ascii", "ignore")
            .decode("ascii")
        )
        continuation_verbs = (
            "continu", "sigue", "sigamos", "sigas", "segui",
            "keep writing", "keep going", "escribe mas", "escribi mas",
        )
        if not any(v in normalized for v in continuation_verbs):
            return None

        # Explicit chapter number mentioned?
        m = re.search(r"(?:cap[ií]tulo|chapter)\s*(\d+)", lower)
        if m:
            num = int(m.group(1))
            if any(c.number == num for c in self.project.chapters):
                return num
            return None  # they named a chapter that doesn't exist — don't guess

        # No explicit number — only treat this as "continue the chapter" if
        # it clearly seems to be about the story (mentions capítulo/chapter/
        # historia/story) or is a short, plainly imperative message like
        # just "continúa" / "sigue" on its own.
        mentions_story = any(
            w in normalized for w in ("cap", "chapter", "historia", "story", "escena", "scene")
        )
        if mentions_story or len(message.strip()) <= 40:
            return max(c.number for c in self.project.chapters)

        return None

    def _run_chat_continue_chapter(self, chapter_num: int) -> None:
        chapter = next((c for c in self.project.chapters if c.number == chapter_num), None)
        if not chapter:
            # Shouldn't happen (detection already checked), but fall back
            # to a normal chat reply rather than doing nothing.
            result = self._run_inference(TaskType.CHAT, self.extra_input, add_to_chat=True)
            if result:
                self._maybe_summarize()
                storage.save_project(self.project)
                self.step_finished.emit("Chat", result)
            return

        self.step_started.emit(f"Continuing Chapter {chapter_num}...")

        tail = chapter.content[-800:].strip() if chapter.content else ""
        model_prompt = (
            f"Continue writing Chapter {chapter_num}: '{chapter.title}' of "
            f"'{self.project.title}'. Continue seamlessly from exactly where "
            "it left off — do not repeat or re-summarize what's already "
            "written, and do not restart the scene."
        )
        if tail:
            model_prompt += f"\n\nThe chapter currently ends with:\n...{tail}"
        if self.extra_input.strip():
            model_prompt += f"\n\nAuthor's note for this continuation: {self.extra_input.strip()}"

        # add_to_chat=False on purpose: the chat transcript should show what
        # the author actually typed (e.g. "sigue con el capítulo 3"), not
        # the long constructed prompt we're actually sending the model.
        result = self._run_inference(
            TaskType.WRITE_CHAPTER, model_prompt, add_to_chat=False, max_tokens=self._content_max_tokens()
        )
        if result:
            sep = "\n\n" if chapter.content.strip() else ""
            chapter.content = (chapter.content.rstrip() + sep + result).strip()
            chapter.reviewed = False  # it grew — worth another look before moving on

            user_msg = ChatMessage(role=MessageRole.USER, content=self.extra_input)
            assistant_msg = ChatMessage(
                role=MessageRole.ASSISTANT,
                content=(
                    f"*(Continued Chapter {chapter_num} — {len(result)} "
                    f"characters added to it.)*\n\n{result}"
                ),
            )
            self.project.chat_messages.append(user_msg)
            self.project.chat_messages.append(assistant_msg)

            self._extract_and_merge_characters(result)
            self._extract_and_merge_world_info(result)
            storage.save_project(self.project)
            self.step_finished.emit(f"Continued Chapter {chapter_num}", result)

    def _run_chat(self) -> None:
        self.step_started.emit("Generating response...")

        target_chapter_num = self._detect_chapter_continuation_request(self.extra_input)
        if target_chapter_num is not None:
            self._run_chat_continue_chapter(target_chapter_num)
            return

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
        """
        Generates the outline, and — same idea as _run_write_chapter's
        multi-pass loop — keeps generating continuations if the model
        stops (runs out of tokens / hits max_tokens) before reaching the
        requested chapter count, instead of silently saving a partial
        outline. Unlike chapter completion (which needs a small LLM call
        to judge "is this scene finished?"), outline completion is
        checked deterministically: does "## Chapter <requested_n>" exist
        yet? That's exactly what Write Book will look for later, so it's
        the right thing to check here too.
        """
        self.step_started.emit("Generating outline...")
        # extra_input carries the chapter-count requirement (and any
        # optional author notes) collected by the "Generate" dialog on the
        # Outline tab — see ui/story.py's GenerateOutlineDialog. It is always
        # ADDED to the base prompt rather than replacing it, so the synopsis
        # is never silently dropped just because a chapter count was given.
        base_prompt = (
            f"Generate a complete chapter-by-chapter outline for '{self.project.title}'.\n"
            "Use the structured format with Objective, Scenes required, and Scenes prohibited for each chapter."
        )
        if self.extra_input:
            base_prompt += f"\n\n{self.extra_input}"
        if self.project.synopsis:
            base_prompt += f"\n\nSynopsis: {self.project.synopsis}"

        requested_n = self._extract_requested_chapter_count(self.extra_input)

        outline_text = ""
        generation_pass = 0
        while generation_pass < MAX_OUTLINE_CONTINUATIONS:
            generation_pass += 1
            if generation_pass == 1:
                logger.info(f"[generate_outline] Generation pass {generation_pass}...")
                generated = self._run_inference(
                    TaskType.GENERATE_OUTLINE, base_prompt, add_to_chat=True, max_tokens=self._content_max_tokens()
                )
            else:
                self.step_started.emit(f"Continuing outline (pass {generation_pass})...")
                logger.info(f"[generate_outline] Continuing outline (pass {generation_pass})...")
                continuation_prompt = self._build_outline_continuation_prompt(outline_text, requested_n)
                generated = self._run_inference(
                    TaskType.GENERATE_OUTLINE, continuation_prompt, add_to_chat=True, max_tokens=self._content_max_tokens()
                )

            if not generated:
                logger.warning(f"[generate_outline] generation pass {generation_pass} returned empty text.")
                break

            # On continuation passes (pass 2+) the model sometimes ignores the
            # "do not repeat" instruction and restarts from Chapter 1. Strip any
            # repeated chapters so only genuinely new content gets appended.
            if generation_pass > 1 and outline_text:
                highest_so_far = max(self._chapter_numbers_in_text(outline_text), default=0)
                generated = self._extract_new_outline_chapters(generated, highest_so_far)
                if not generated:
                    logger.warning(
                        f"[generate_outline] pass {generation_pass} produced only repeated "
                        "chapters — skipping this response."
                    )
                    break

            outline_text = (
                (outline_text.rstrip() + "\n\n" + generated.strip()).strip()
                if outline_text else generated.strip()
            )

            # Keep project.outline in sync with the accumulated text so that
            # build_context_for_model picks up what already exists when it
            # builds the system prompt for the next continuation pass.
            # Without this, the system prompt for pass 2+ still shows the OLD
            # (or empty) outline, giving the model authority to restart from
            # Chapter 1 instead of continuing from where it stopped.
            self.project.outline = outline_text

            if not requested_n:
                # No explicit chapter count to check against (e.g. outline
                # was generated some other way) — one pass is the best we
                # can do without a target.
                break

            actual_numbers = self._chapter_numbers_in_text(outline_text)
            if actual_numbers and max(actual_numbers) >= requested_n:
                logger.info(
                    f"[generate_outline] Reached requested chapter count "
                    f"({len(actual_numbers)}/{requested_n}) after {generation_pass} pass(es)."
                )
                break

            if generation_pass >= MAX_OUTLINE_CONTINUATIONS:
                logger.warning(
                    f"[generate_outline] Reached MAX_OUTLINE_CONTINUATIONS="
                    f"{MAX_OUTLINE_CONTINUATIONS} with only "
                    f"{len(actual_numbers)}/{requested_n} chapters. Saving as-is."
                )
                break

            logger.info("[generate_outline] Outline not complete yet. Generating continuation...")
            self._clear_chat_messages_for_continuation()

        if outline_text:
            self.project.outline = outline_text
            self._extract_and_merge_characters(outline_text)
            self._extract_and_merge_world_info(outline_text)

            # Final sanity check — covers the (rare) case where even
            # MAX_OUTLINE_CONTINUATIONS passes weren't enough, or the model
            # overshot/undershot the count despite reaching it.
            actual_numbers = self._outline_chapter_numbers()
            if requested_n and actual_numbers and len(actual_numbers) != requested_n:
                note = (
                    f"\n\n---\n**Note:** {requested_n} chapters were requested, but this "
                    f"outline has {len(actual_numbers)}. You can edit it directly on the "
                    "Outline tab, or click Generate again."
                )
                if (
                    self.project.chat_messages
                    and self.project.chat_messages[-1].role == MessageRole.ASSISTANT
                ):
                    self.project.chat_messages[-1].content += note

            storage.save_project(self.project)
            self.step_finished.emit("Outline", outline_text)

    def _extract_requested_chapter_count(self, text: str) -> Optional[int]:
        """Pulls the chapter count out of the requirement text the Outline
        tab's Generate dialog inserts into extra_input (see
        GenerateOutlineDialog in ui/story.py), so we can sanity-check the
        model's output against what was actually asked for."""
        if not text:
            return None
        match = re.search(r"EXACTLY\s+(\d+)\s+chapters", text, re.IGNORECASE)
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                return None
        return None

    @staticmethod
    def _extract_new_outline_chapters(generated: str, highest_already: int) -> str:
        """
        Strip repeated chapters from a continuation response.

        When the model is asked to continue from Chapter N+1 it sometimes
        ignores the "do not repeat" instruction and restarts from Chapter 1.
        This filters the response to only keep headings with a chapter number
        strictly greater than highest_already, discarding everything before the
        first genuinely new heading (including preamble text and repeated
        chapter entries).

        If the model responded correctly and started at Chapter N+1, the text
        passes through unchanged. If it only repeated old chapters (no new
        content at all) an empty string is returned, which the caller treats as
        an empty generation and breaks the continuation loop.
        """
        if not generated.strip():
            return ""
        lines = generated.split("\n")
        new_lines = []
        skip_until_new = True
        for line in lines:
            if skip_until_new:
                m = re.match(r"^\s*##\s*Chapter\s+(\d+)\b", line, re.IGNORECASE)
                if m:
                    chapter_num = int(m.group(1))
                    if chapter_num > highest_already:
                        skip_until_new = False
                        new_lines.append(line)
                # else: repeated chapter heading or pre-chapter preamble — skip
            else:
                new_lines.append(line)
        return "\n".join(new_lines).strip()

    def _build_outline_continuation_prompt(self, outline_so_far: str, requested_n: Optional[int]) -> str:
        """
        Continuation prompt for generate-outline passes 2+.

        The system prompt deliberately omits project.outline for
        GENERATE_OUTLINE tasks (to avoid contaminating a fresh generation
        with a stale prior outline). For continuation passes this works
        against us: the model has no authoritative view of what already
        exists and may restart from Chapter 1.

        We solve this by embedding the full accumulated outline text
        directly in the user message, so the model sees exactly what it
        has written so far regardless of what the system prompt includes.
        """
        numbers = self._chapter_numbers_in_text(outline_so_far)
        highest_so_far = max(numbers) if numbers else 0
        next_num = highest_so_far + 1
        target = f" through Chapter {requested_n}" if requested_n else ""

        parts = [
            f"Continue the chapter-by-chapter outline for '{self.project.title}'.",
            f"The outline already covers Chapters 1–{highest_so_far}. "
            f"Do NOT rewrite or repeat any of them.",
            f'Pick up exactly at "## Chapter {next_num}"{target} and continue '
            f"until Chapter {requested_n} is complete." if requested_n else
            f'Pick up exactly at "## Chapter {next_num}" and continue.',
            "Use the same structured format for each chapter: "
            "Objective, Scenes required, Scenes prohibited.",
            "",
            f"Outline written so far (do not repeat this):\n{outline_so_far.strip()}",
        ]
        return "\n\n".join(parts).strip()

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

    def _extract_chapter_outline_section(self, chapter_number: int) -> str:
        """
        Pull out just THIS chapter's entry from the full outline (expects
        "## Chapter N: Title" headings, which is exactly the format
        GENERATE_OUTLINE's instructions ask the model to produce).
        Returns "" if no matching heading is found.
        """
        outline = self.project.outline
        if not outline:
            return ""
        lines = outline.split("\n")
        target = f"## Chapter {chapter_number}"
        next_marker = f"## Chapter {chapter_number + 1}"
        capture = False
        result = []
        for line in lines:
            if target in line and (
                len(line) == len(target)
                or not line[len(target):len(target) + 1].isdigit()
            ):
                capture = True
            elif next_marker in line and capture:
                break
            if capture:
                result.append(line)
        return "\n".join(result).strip()

    @staticmethod
    def _chapter_numbers_in_text(text: str) -> list[int]:
        """
        Chapter numbers found in any block of "## Chapter N" headings.
        Shared by outline generation/continuation (working off a text
        buffer that may not be saved to the project yet) and
        _outline_chapter_numbers() (working off the saved outline).
        """
        numbers = set()
        for match in re.finditer(r"^\s*##\s*Chapter\s+(\d+)\b", text or "", re.IGNORECASE | re.MULTILINE):
            try:
                numbers.add(int(match.group(1)))
            except ValueError:
                continue
        return sorted(numbers)

    def _outline_chapter_numbers(self) -> list[int]:
        return self._chapter_numbers_in_text(self.project.outline or "")

    def _outline_max_chapter_number(self) -> int:
        numbers = self._outline_chapter_numbers()
        return max(numbers, default=0)

    def _outline_has_chapter(self, chapter_number: int) -> bool:
        return chapter_number in set(self._outline_chapter_numbers())

    def _next_chapter_number(self) -> int:
        """
        Single source of truth for the next chapter to write.

        This matches the logic used by _run_write_chapter(): always advance
        from whichever chapter number is actually highest on disk, and never
        invent a separate outline-driven notion of "next".
        """
        highest_existing = max((c.number for c in self.project.chapters), default=0)
        return max(self.project.current_chapter, highest_existing) + 1

    def _clear_temporary_chat_history(self) -> None:
        self.project.chat_messages = []
        storage.save_project(self.project)

    def _clear_chat_messages_for_continuation(self) -> None:
        """
        Clear transient chat history between continuation passes.

        This keeps the next pass anchored on the persistent project state
        plus the partial chapter text, instead of allowing prior continuation
        turns to accumulate in chat_messages.
        """
        if self.project.chat_messages:
            self.project.chat_messages = []
            storage.save_project(self.project)

    def _reload_project_from_storage(self) -> bool:
        refreshed = storage.load_project(self.project.id)
        if refreshed is None:
            logger.error(f"[write_book] Could not reload project '{self.project.id}' from storage.")
            self.error_occurred.emit("Could not reload project from storage.")
            return False
        self.project = refreshed
        return True

    def _build_chapter_evaluation_prompt(
        self,
        chapter_num: int,
        chapter_text: str,
        chapter_goal: str,
    ) -> str:
        outline_context = self._extract_chapter_outline_section(chapter_num)
        parts = [
            f"Decide whether Chapter {chapter_num} of '{self.project.title}' is complete.",
            f"You are evaluating ONLY Chapter {chapter_num} — not the whole book, "
            f"not Chapter 1, not any other chapter.",
            "Respond with exactly one token: true or false.",
            "No punctuation. No quotes. No markdown. No JSON. No explanation. No extra text.",
            "true  → Chapter is complete and should stop.",
            "false → Chapter is not complete and must continue.",
            "",
            f"Chapter {chapter_num} Goal:\n{chapter_goal.strip() or '(none)'}",
        ]
        if outline_context:
            parts.append(f"Chapter {chapter_num} Outline Entry:\n{outline_context}")
        parts.append(f"Chapter {chapter_num} Draft:\n{chapter_text}")
        return "\n\n".join(parts).strip()

    def _build_chapter_continuation_prompt(
        self,
        chapter_num: int,
        chapter_text: str,
        chapter_goal: str,
    ) -> str:
        # Use a longer tail so the model has more overlap context and is
        # less likely to invent a scene-break or repeat a sentence already
        # written just before the cut-off point.
        tail = chapter_text[-2000:].strip()

        # List already-completed chapters so the model cannot confuse which
        # chapter is currently in progress or restart from Chapter 1.
        completed = sorted(c.number for c in self.project.chapters if c.number != chapter_num and c.content)
        if completed:
            completed_note = (
                f"Chapters already fully written and saved: {', '.join(str(n) for n in completed)}. "
                f"Do NOT rewrite any of them."
            )
        else:
            completed_note = ""

        parts = [
            f"You are continuing Chapter {chapter_num} of '{self.project.title}'.",
            (
                f"The text below shows where Chapter {chapter_num} currently ends. "
                "Your job is to pick up immediately after the last word shown and keep writing. "
                "Do NOT rewrite or paraphrase anything already written. "
                "Do NOT add a heading, title, or chapter number. "
                "Do NOT restart the scene. "
                "Just continue the prose seamlessly."
            ),
        ]

        if completed_note:
            parts.append(completed_note)

        parts += [
            f"Chapter {chapter_num} Goal (what must be accomplished before the chapter ends):"
            f"\n{chapter_goal.strip() or '(none)'}",
            f"--- END OF CHAPTER {chapter_num} SO FAR ---\n{tail}\n--- CONTINUE FROM HERE ---",
        ]
        return "\n\n".join(parts).strip()

    def _parse_completion_evaluation(self, text: str) -> dict:
        normalized = text.strip().lower()
        if normalized == "true":
            return {"valid": True, "completed": True}
        if normalized == "false":
            return {"valid": True, "completed": False}
        return {"valid": False, "completed": False}

    def _evaluate_chapter_completion(
        self,
        chapter_num: int,
        chapter_text: str,
        chapter_goal: str,
    ) -> dict:
        prompt = self._build_chapter_evaluation_prompt(chapter_num, chapter_text, chapter_goal)
        for attempt in range(1, MAX_COMPLETION_EVAL_RETRIES + 1):
            evaluation = self._run_inference(
                TaskType.REVIEW_CHAPTER,
                prompt,
                add_to_chat=False,
                max_tokens=8,
            )
            parsed = self._parse_completion_evaluation(evaluation)
            if parsed["valid"]:
                logger.info(
                    f"[write_chapter] evaluation: {evaluation.strip().lower()}"
                )
                return parsed
            logger.warning(
                f"[write_chapter] invalid completion evaluation output on attempt {attempt}: "
                f"{evaluation.strip()!r}"
            )
        logger.warning("[write_chapter] completion evaluator failed; defaulting to false.")
        return {"valid": False, "completed": False}

    def _run_write_chapter(self) -> None:
        # Never trust project.current_chapter alone — if it's stale (e.g.
        # left over from before a chapter was manually deleted, or set
        # incorrectly by some other UI action) always continue forward
        # from whichever chapter number is actually highest on disk.
        chapter_num = self._next_chapter_number()
        self.step_started.emit(f"Writing Chapter {chapter_num}...")

        prompt_parts = [f"Write Chapter {chapter_num} of '{self.project.title}'."]

        if self.project.outline:
            specific = self._extract_chapter_outline_section(chapter_num)
            if specific:
                prompt_parts.append(
                    "This chapter's planned outline entry is binding. "
                    "Follow it exactly and do not expand beyond what it states.\n"
                    f"{specific}"
                )
            else:
                # Couldn't find a "## Chapter N" heading for this number
                # (outline format may differ) — fall back to the full
                # outline so the model still treats it as authoritative.
                prompt_parts.append(
                    f"Full outline (authoritative source of truth for Chapter {chapter_num}):"
                    f"\n{self.project.outline}"
                )

        if chapter_num > 1:
            prev = next(
                (c for c in self.project.chapters if c.number == chapter_num - 1), None
            )
            if prev and prev.content:
                tail = prev.content[-800:].strip()
                prompt_parts.append(
                    f"End of the previous chapter for continuity only:\n...{tail}"
                )

        prompt_parts.append(
            "Rules:\n"
            "- The outline is binding and must be followed exactly.\n"
            "- Do not introduce important events that are not in the outline.\n"
            "- Do not bring in later-chapter events early.\n"
            "- Do not resolve any conflict unless the outline resolves it here.\n"
            "- Do not introduce major characters before they appear in the outline.\n"
            "- If the outline leaves something open, end the chapter open.\n"
            "- Your job is to develop the outlined scenes, not invent a different story."
        )

        # Author profile: style preferences + intent signals relevant to prose.
        # The system prompt already carries the full Creative Direction section;
        # repeating the concise fragments here reinforces them at the exact
        # point of generation without significant token cost.
        style_frag = self.project.writing_style.to_prompt_fragment()
        if style_frag:
            prompt_parts.append(f"Style to apply:\n{style_frag}")

        intent = self.project.author_intent
        intent_lines = []
        if intent.emotional_journey:
            intent_lines.append(f"Emotional tone to sustain in this chapter: {intent.emotional_journey}")
        if intent.avoid:
            intent_lines.append(f"Avoid entirely: {intent.avoid}")
        if intent_lines:
            prompt_parts.append("\n".join(intent_lines))

        if self.extra_input:
            prompt = self.extra_input
        else:
            prompt = "\n\n".join(prompt_parts)

        chapter_goal = self._extract_chapter_outline_section(chapter_num) or prompt
        chapter_text = ""
        generation_pass = 0
        evaluation = {"completed": True, "confidence": 100, "reason": "", "next": ""}

        while generation_pass < MAX_CONTINUATIONS:
            generation_pass += 1
            if generation_pass == 1:
                self.step_started.emit(f"Generation pass {generation_pass}...")
                logger.info(f"[write_chapter] Generation pass {generation_pass}...")
                generated = self._run_inference(
                    TaskType.WRITE_CHAPTER, prompt, add_to_chat=True, max_tokens=self._content_max_tokens()
                )
            else:
                self.step_started.emit(f"Generating continuation (pass {generation_pass})...")
                logger.info(f"[write_chapter] Generating continuation (pass {generation_pass})...")
                # Clear chat history BEFORE building the continuation prompt so
                # build_context_for_model does not see the previous pass's
                # user+assistant messages in the context window. Those messages
                # already contain the chapter tail embedded in the prompt, so
                # including them again causes the model to see — and repeat —
                # the same text twice.
                self._clear_chat_messages_for_continuation()
                continuation_prompt = self._build_chapter_continuation_prompt(
                    chapter_num,
                    chapter_text,
                    chapter_goal,
                )
                # add_to_chat=False: the continuation prompt embeds the chapter
                # tail directly, so accumulating it in chat history would cause
                # it to appear again in the context of the next pass.
                generated = self._run_inference(
                    TaskType.WRITE_CHAPTER, continuation_prompt, add_to_chat=False, max_tokens=self._content_max_tokens()
                )

            if not generated:
                logger.warning(f"[write_chapter] generation pass {generation_pass} returned empty text.")
                break

            chapter_text = (chapter_text.rstrip() + "\n\n" + generated.strip()).strip() if chapter_text else generated.strip()

            evaluation = self._evaluate_chapter_completion(
                chapter_num,
                chapter_text,
                chapter_goal,
            )

            if evaluation["completed"]:
                logger.info(
                    f"[write_chapter] Chapter completed after {generation_pass} generation pass(es)."
                )
                break

            if generation_pass >= MAX_CONTINUATIONS:
                logger.warning(
                    f"[write_chapter] Reached MAX_CONTINUATIONS={MAX_CONTINUATIONS}. "
                    "Saving chapter as-is."
                )
                break

            logger.info(
                "[write_chapter] Chapter not complete yet. Generating continuation..."
            )

        if chapter_text:
            existing = next((c for c in self.project.chapters if c.number == chapter_num), None)
            if existing:
                existing.content = chapter_text
                existing.reviewed = False
            else:
                ch = Chapter(
                    number=chapter_num,
                    title=f"Chapter {chapter_num}",
                    content=chapter_text,
                )
                self.project.chapters.append(ch)

            self._extract_and_merge_characters(chapter_text)
            self._extract_and_merge_world_info(chapter_text)
            storage.save_project(self.project)
            self.step_finished.emit(f"Chapter {chapter_num}", chapter_text)

    def _run_write_book(self) -> None:
        outline_numbers = self._outline_chapter_numbers()
        total = len(outline_numbers) if outline_numbers else max(1, len(self.project.chapters) + 1)
        max_outline_chapter = max(outline_numbers, default=0)
        self.step_started.emit(f"Writing Book (0/{total})...")
        written = 0
        while not self._cancelled:
            if not self._reload_project_from_storage():
                break
            # Refresh outline metadata after reload so the cap always
            # reflects the actual saved outline (in case it was edited).
            current_outline_numbers = self._outline_chapter_numbers()
            if current_outline_numbers:
                max_outline_chapter = max(current_outline_numbers)
                total = len(current_outline_numbers)
            total = total or max(1, len(self.project.chapters) + 1)
            pending = self._next_chapter_number()
            if pending <= 0:
                break
            if self.project.outline:
                # Stop if the outline doesn't contain this chapter number at
                # all, OR if the chapter number exceeds the highest outlined
                # chapter. Using both checks covers outlines that use
                # non-sequential numbering AND the common case where the model
                # generated fewer headings than requested (max_outline_chapter
                # would be 0 in that edge case, making the old "> 0 and >"
                # guard silently skip — now we check the heading set directly).
                if not self._outline_has_chapter(pending):
                    logger.info(
                        f"[write_book] Chapter {pending} has no outline entry "
                        f"(outline covers: {current_outline_numbers}). Stopping."
                    )
                    break
                if max_outline_chapter and pending > max_outline_chapter:
                    break
            self.project.current_chapter = pending - 1
            logger.info(f"[write_book] Writing Chapter {pending}/{total}.")
            self.step_started.emit(f"Writing Chapter {pending}/{total}...")
            self._run_write_chapter()
            written += 1

            if self._cancelled:
                break

            # Update Story Memory for the chapter we JUST wrote, before moving
            # on to the next one. Without this, project.memory (which every
            # generation's system prompt includes) stays one chapter behind —
            # the next chapter would be written without knowing what just
            # happened in this one.
            wrote_chapter = any(c.number == pending for c in self.project.chapters)
            if wrote_chapter:
                # _run_write_chapter() never touches current_chapter itself,
                # so it's still (pending - 1) from the line above. Point it at
                # the chapter we actually just finished before calling
                # _run_update_memory(), which reads current_chapter to decide
                # which chapter to summarize.
                self.project.current_chapter = pending
                logger.info(f"[write_book] Updating Story Memory for Chapter {pending}.")
                self.step_started.emit(f"Updating Story Memory for Chapter {pending}...")
                self._run_update_memory()
            else:
                logger.warning(
                    f"[write_book] Chapter {pending} was not produced "
                    "(empty generation?) — skipping memory update for it."
                )

            if self._cancelled:
                break
            if self._stop_after_current_chapter:
                logger.info("[write_book] Stop requested after current chapter.")
                break
            if not self._reload_project_from_storage():
                break
            next_pending = self._next_chapter_number()
            if next_pending <= pending:
                break
            if self.project.outline:
                if not self._outline_has_chapter(next_pending):
                    logger.info(
                        f"[write_book] Next chapter {next_pending} has no outline entry. Stopping."
                    )
                    break
                if max_outline_chapter and next_pending > max_outline_chapter:
                    break
            if next_pending is not None:
                logger.info(f"[write_book] Clearing chat before Chapter {pending}.")
                self._clear_temporary_chat_history()
                self.clear_chat_requested.emit()
                self.step_started.emit("Clearing chat...")

        if written == 0:
            logger.info("[write_book] No pending outline chapters found.")
        else:
            logger.info(f"[write_book] Finished writing {written} chapter(s).")

    def _run_review_chapter(self) -> None:
        chapter_num = self.project.current_chapter
        if chapter_num == 0:
            chapter_num = len(self.project.chapters)

        self.step_started.emit(f"Reviewing Chapter {chapter_num}...")

        chapter = next((c for c in self.project.chapters if c.number == chapter_num), None)
        if not chapter:
            self.error_occurred.emit(f"Chapter {chapter_num} not found.")
            return

        review_parts = [f"Review Chapter {chapter_num}: '{chapter.title}'\n\n{chapter.content}"]

        # Give the reviewer the author's intent and style so it evaluates the
        # chapter against the book's actual goals, not just generic writing quality.
        style_frag = self.project.writing_style.to_prompt_fragment()
        if style_frag:
            review_parts.append(f"Style preferences to check against:\n{style_frag}")

        intent = self.project.author_intent
        intent_lines = []
        if intent.emotional_journey:
            intent_lines.append(f"Intended emotional experience: {intent.emotional_journey}")
        if intent.lasting_impression:
            intent_lines.append(f"What the reader should take away: {intent.lasting_impression}")
        if intent.themes:
            intent_lines.append(f"Themes to serve: {intent.themes}")
        if intent.avoid:
            intent_lines.append(f"Elements to flag if present: {intent.avoid}")
        if intent_lines:
            review_parts.append("Author's creative intent for context:\n" + "\n".join(intent_lines))

        prompt = "\n\n".join(review_parts)
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

        if self.extra_input:
            prompt = self.extra_input
        else:
            rewrite_parts = [
                f"Chapter {chapter_num}: '{chapter.title}'\n\n"
                f"Current content:\n{chapter.content}\n\n"
                f"Review feedback to address:\n{chapter.last_review}"
            ]
            style_frag = self.project.writing_style.to_prompt_fragment()
            if style_frag:
                rewrite_parts.append(f"Style to preserve:\n{style_frag}")

            intent = self.project.author_intent
            intent_lines = []
            if intent.emotional_journey:
                intent_lines.append(f"Emotional tone to sustain: {intent.emotional_journey}")
            if intent.avoid:
                intent_lines.append(f"Avoid entirely: {intent.avoid}")
            if intent_lines:
                rewrite_parts.append("\n".join(intent_lines))

            prompt = "\n\n".join(rewrite_parts)

        result = self._run_inference(
            TaskType.REWRITE_CHAPTER, prompt, add_to_chat=True, max_tokens=self._content_max_tokens()
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

        chapter = next(
            (c for c in self.project.chapters if c.number == chapter_num),
            None,
        )

        if not chapter:
            self.error_occurred.emit("No chapter found to extract memory from.")
            return

        existing_memory = self.project.memory.strip() or "(empty)"

        world = self.project.world.strip() or "(none)"

        characters = "\n".join(
            f"- {c.name}: {c.description}"
            for c in self.project.characters
        ) or "(none)"

        prompt = f"""
    You are updating the STORY MEMORY of a novel.

    IMPORTANT:

    Story Memory is NOT a wiki.

    Story Memory is NOT a character database.

    Story Memory is NOT worldbuilding.

    Those are stored separately.

    ==================================================
    WORLD INFORMATION
    ==================================================

    {world}

    ==================================================
    CHARACTERS
    ==================================================

    {characters}

    ==================================================
    CURRENT STORY MEMORY
    ==================================================

    {existing_memory}

    ==================================================
    NEW CHAPTER
    ==================================================

    Chapter {chapter_num}: {chapter.title}

    {chapter.content}

    ==================================================
    YOUR JOB
    ==================================================

    Update ONLY the Story Memory.

    DO NOT repeat information already contained in:
    - World
    - Characters

    Do NOT describe:
    - physical appearance
    - locations already documented
    - permanent abilities
    - lore
    - history
    - personality unless it has changed

    Instead keep only information needed to continue writing the next chapter.

    The Story Memory should contain ONLY:

    # Current Location

    # Current Situation

    # Active Goals

    # Important Relationship Changes

    # Inventory / Status Changes

    # Unresolved Plot Threads

    Remove information that is no longer relevant.

    Keep unresolved information from previous chapters.

    Never delete ongoing plot threads.

    Output ONLY the updated Story Memory.

    Do NOT explain your reasoning.
    Do NOT use markdown code blocks.
    """

        result = self._run_inference(
            TaskType.UPDATE_MEMORY,
            prompt,
            add_to_chat=False,
            max_tokens=1500,
        )

        if result:
            self.project.memory = result.strip()
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
        language = self._response_language()
        if language:
            system += f" Write the summary in {language}."
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
    clear_chat_requested = Signal()

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
        self.worker.clear_chat_requested.connect(self.clear_chat_requested)

    def run(self) -> None:
        self.worker.run()

    def cancel(self) -> None:
        self.worker.cancel()

    def request_stop_after_current_chapter(self) -> None:
        self.worker.request_stop_after_current_chapter()