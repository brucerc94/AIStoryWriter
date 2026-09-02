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
import time
import traceback
import unicodedata
from typing import Optional

from PySide6.QtCore import QObject, QThread, Signal

from engine.chat import get_engine
from engine.context import (
    build_context_for_model,
    build_review_context_for_model,
    build_system_prompt,
    estimate_messages_tokens,
    build_summarization_prompt,
    format_characters_block,
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
)
from engine import storage
from engine import prompts
from engine import change_chapter
from engine.character_dedup import find_existing_character, merge_nonempty_fields


logger = logging.getLogger("workflow")

# Marker prefixes for structured requests piggybacked onto `extra_input`.
# Only the matching _run_* handler inspects each marker.

# ── Markdown section utilities ──
# Text-processing helpers for merging world facts into the World & Setting
# document by section heading. No SEARCH/REPLACE logic.

import re as _re

_HEADING_RE_WF = _re.compile(r"^(#{1,6})\s*(.+?)\s*$", _re.MULTILINE)

WORLD_ALLOWED_SECTIONS = ("Geography", "Culture & Customs", "Relevant History")
_WORLD_SECTION_ALIASES = {
    "geography": "Geography",
    "geography & key locations": "Geography",
    "locations": "Geography",
    "key locations": "Geography",
    "climate": "Geography",
    "terrain": "Geography",
    "culture": "Culture & Customs",
    "culture & customs": "Culture & Customs",
    "political & social structure": "Culture & Customs",
    "factions": "Culture & Customs",
    "economies": "Culture & Customs",
    "languages": "Culture & Customs",
    "social structure": "Culture & Customs",
    "history": "Relevant History",
    "history relevant to the plot": "Relevant History",
    "relevant history": "Relevant History",
    "time period & technology": "Relevant History",
}
_WORLD_DISALLOWED_SECTIONS = {
    "magic", "magic / power systems", "power systems",
    "technology", "technology & time period",
}


def _wf_is_world_document(document: str) -> bool:
    return bool(_re.search(r"^#\s+World\s*$", document or "", _re.MULTILINE | _re.IGNORECASE))


def _wf_canonical_world_section(title: str) -> "Optional[str]":
    normalized = _re.sub(r"\s+", " ", (title or "").strip().lower())
    if normalized in _WORLD_DISALLOWED_SECTIONS:
        return None
    if normalized in _WORLD_SECTION_ALIASES:
        return _WORLD_SECTION_ALIASES[normalized]
    if normalized in {s.lower() for s in WORLD_ALLOWED_SECTIONS}:
        return next(s for s in WORLD_ALLOWED_SECTIONS if s.lower() == normalized)
    return None


def _wf_headings(document: str) -> "list[tuple[int, int, str]]":
    return [
        (m.start(), len(m.group(1)), m.group(2).strip())
        for m in _HEADING_RE_WF.finditer(document)
    ]


def _wf_merge_world_sections(document: str) -> str:
    if not _wf_is_world_document(document):
        return document
    headings = _wf_headings(document)
    if not headings:
        return document.strip() + "\n"
    buckets: dict = {name: [] for name in WORLD_ALLOWED_SECTIONS}
    for i, (start, level, title) in enumerate(headings):
        if level != 2:
            continue
        end = len(document)
        for start2, level2, _ in headings[i + 1:]:
            if level2 <= level:
                end = start2
                break
        body = document[start:end]
        body = _re.sub(r"^##\s*.+?\s*\n", "", body, count=1).strip()
        if not body:
            continue
        canonical = _wf_canonical_world_section(title)
        if canonical:
            buckets[canonical].append(body)
    lines = ["# World", ""]
    for name in WORLD_ALLOWED_SECTIONS:
        lines.append(f"## {name}")
        if buckets[name]:
            lines.append("\n".join(x.strip() for x in buckets[name] if x.strip()))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _wf_find_section_bounds(document: str, section_name: str) -> "Optional[tuple[int, int, int]]":
    headings = _wf_headings(document)
    target = section_name.strip().lower()
    for i, (start, level, title) in enumerate(headings):
        if title.lower() == target:
            end = len(document)
            for start2, level2, _ in headings[i + 1:]:
                if level2 <= level:
                    end = start2
                    break
            return start, end, level
    return None


def _wf_find_relevant_section(document: str, query: str, min_overlap: int = 1) -> "Optional[str]":
    if _wf_is_world_document(document):
        canonical = _wf_merge_world_sections(document)
        parts = []
        for name in WORLD_ALLOWED_SECTIONS:
            bounds = _wf_find_section_bounds(canonical, name)
            if bounds:
                start, end, _ = bounds
                parts.append(canonical[start:end].strip())
        return "\n\n".join(parts).strip() or None
    if not document.strip():
        return None
    headings = _wf_headings(document)
    if not headings:
        return None
    query_words = set(_re.findall(r"[a-zA-ZÀ-ÿ]{4,}", query.lower()))
    if not query_words:
        return None
    best_body = None
    best_score = 0
    for i, (start, level, _title) in enumerate(headings):
        end = len(document)
        for start2, level2, _ in headings[i + 1:]:
            if level2 <= level:
                end = start2
                break
        body = document[start:end]
        body_words = set(_re.findall(r"[a-zA-ZÀ-ÿ]{4,}", body.lower()))
        score = len(query_words & body_words)
        if score > best_score:
            best_score = score
            best_body = body
    if best_body is not None and best_score >= min_overlap:
        return best_body.strip()
    return None


def _wf_ensure_sections(document: str, section_names: list) -> str:
    doc = document if document and document.strip() else ""
    if not _re.search(r"^#\s+\S", doc, _re.MULTILINE):
        doc = ("# World\n\n" + doc.strip() + "\n") if doc.strip() else "# World\n"
    if _wf_is_world_document(doc):
        return _wf_merge_world_sections(doc)
    existing_titles = {
        m.group(1).strip().lower()
        for m in _re.finditer(r"^##\s*(.+?)\s*$", doc, _re.MULTILINE)
    }
    additions = [
        f"\n## {name}\n" for name in section_names
        if name.strip().lower() not in existing_titles
    ]
    if additions:
        doc = doc.rstrip() + "\n" + "\n".join(additions) + "\n"
    return doc


def _wf_split_markdown_sections(text: str) -> "list[tuple[str, str]]":
    if not text or not text.strip():
        return []
    headings = _wf_headings(text)
    if not headings:
        return [("", text.strip())]
    sections = []
    if headings[0][0] > 0:
        pre = text[: headings[0][0]].strip()
        if pre:
            sections.append(("", pre))
    for i, (start, _level, title) in enumerate(headings):
        end = headings[i + 1][0] if i + 1 < len(headings) else len(text)
        sections.append((title, text[start:end].strip()))
    return sections


def _wf_merge_markdown_document(existing: str, new_text: str, default_sections: list) -> str:
    doc = _wf_ensure_sections(existing, default_sections)
    if _wf_is_world_document(new_text):
        normalized_new = _wf_merge_world_sections(new_text)
    else:
        normalized_new = new_text
    if _wf_is_world_document(doc):
        for title, body in _wf_split_markdown_sections(normalized_new):
            canonical = _wf_canonical_world_section(title)
            if not canonical:
                continue
            body_content = _re.sub(r"^#{1,6}\s*.+?\s*\n", "", body, count=1).strip()
            if not body_content:
                continue
            bounds = _wf_find_section_bounds(doc, canonical)
            if not bounds:
                continue
            start, end, _level = bounds
            existing_body = doc[start:end]
            additions = [line.strip() for line in body_content.splitlines() if line.strip()]
            new_lines = []
            existing_norm = {_re.sub(r"\s+", " ", line).strip().lower() for line in existing_body.splitlines() if line.strip()}
            for line in additions:
                norm = _re.sub(r"\s+", " ", line).strip().lower()
                if norm and norm not in existing_norm:
                    new_lines.append(line)
                    existing_norm.add(norm)
            if not new_lines:
                continue
            merged_section = existing_body.rstrip("\n") + "\n" + "\n".join(new_lines) + "\n"
            doc = doc[:start] + merged_section + doc[end:]
        return _wf_merge_world_sections(doc)
    for title, body in _wf_split_markdown_sections(normalized_new):
        if not title:
            doc = doc.rstrip() + "\n\n" + body.strip() + "\n"
            continue
        body_content = _re.sub(r"^#{1,6}\s*.+?\s*\n", "", body, count=1).strip()
        if not body_content:
            continue
        bounds = _wf_find_section_bounds(doc, title)
        if bounds:
            start, end, _level = bounds
            merged_section = doc[start:end].rstrip("\n") + "\n" + body_content + "\n"
            doc = doc[:start] + merged_section + doc[end:]
        else:
            doc = doc.rstrip() + f"\n\n## {title}\n{body_content}\n"
    return doc.strip() + "\n"


OUTLINE_SUGGESTION_MARKER = "__AI_STORY_WRITER_OUTLINE_SUGGESTION__"
OUTLINE_EXTEND_MARKER = "__AI_STORY_WRITER_OUTLINE_EXTEND__"
CHAT_CHAPTER_ATTACHMENT_MARKER = "__AI_STORY_WRITER_CHAT_CHAPTER_ATTACHMENT__"


class _StepTimer:
    """
Context manager that logs elapsed time for a labeled block."""
    def __init__(self, label: str):
        self.label = label
        self._start = 0.0

    def __enter__(self) -> "_StepTimer":
        self._start = time.monotonic()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        elapsed = time.monotonic() - self._start
        if exc_type is None:
            logger.info(f"[timing] {self.label}: {elapsed:.2f}s")
        else:
            logger.info(f"[timing] {self.label}: {elapsed:.2f}s (raised {exc_type.__name__})")
        return False

MAX_CONTINUATIONS = 3
MAX_COMPLETION_EVAL_RETRIES = 2
# Allow additional passes for long outlines (a 40-chapter outline won't fit in one pass).
MAX_OUTLINE_CONTINUATIONS = 8

# Trailing existing-outline chapters sent as reference context per Extend Outline pass.
# Kept small to avoid context overflow on long outlines.
EXTEND_OUTLINE_REFERENCE_TAIL_CHAPTERS = 2

# Trailing accumulated-chapters sent as reference within an extension run.
# Prevents context overflow when several large chapters are generated in sequence.
EXTEND_OUTLINE_ACCUMULATED_TAIL_CHAPTERS = 1

# Default section skeleton for World & Setting, created lazily on first update.
DEFAULT_WORLD_SECTIONS = ["Geography", "Kingdoms", "Factions", "Magic", "History"]


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
        include_story_context: bool = True,
    ) -> None:
        super().__init__()
        self.project = project
        self.task = task
        self.extra_input = extra_input
        self.settings = settings
        self.include_story_context = include_story_context
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

    def _log_prompt_if_enabled(self, label: str, messages: list[dict]) -> None:
        """
Log the full prompt when the "Show full prompt in log" setting is enabled; no-op otherwise."""
        if not (self.settings and getattr(self.settings, "log_full_prompts", False)):
            return
        parts = [f"[prompt:{label}]"]
        for m in messages:
            parts.append(f"--- {m.get('role', '?')} ---\n{m.get('content', '')}")
        logger.info("\n".join(parts))

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
            with _StepTimer(f"model_load({task.value})"):
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

    def _task_top_p(self, task: TaskType) -> float:
        """Per-task Top P, set from the Models tab next to that task's model."""
        return self.project.task_temperatures.get_top_p(task)

    def _task_top_k(self, task: TaskType) -> int:
        """Per-task Top K, set from the Models tab next to that task's model."""
        return self.project.task_temperatures.get_top_k(task)

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
        include_story_context: bool = True,
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
            include_story_context=include_story_context,
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
                task, user_message, system_prompt, budget, reply_reserved,
                include_story_context=self.include_story_context,
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
        top_p = self._task_top_p(task)
        top_k = self._task_top_k(task)
        logger.info(
            f"[{task.value}] running inference: {len(messages)} messages, "
            f"temperature={temperature}, top_p={top_p}, top_k={top_k}, "
            f"max_tokens={effective_max_tokens}"
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

        # Print the full prompt to stdout for debugging.
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
        self._log_prompt_if_enabled(task.value, messages)
        try:
            with _StepTimer(f"generate({task.value})"):
                result = engine.generate(
                    messages=messages,
                    max_tokens=effective_max_tokens,
                    temperature=temperature,
                    top_p=top_p,
                    top_k=top_k,
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
Max tokens per generation pass for outline and chapter tasks. Falls back to 4000."""
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
Extract named characters from generated text and add new ones to project.characters.
        Emits step_started to keep the status bar active; adds no chat bubble."""
        if not source_text or not source_text.strip():
            return
        _t_start = time.monotonic()
        if not self._load_model_for_task(TaskType.UPDATE_MEMORY):
            logger.warning("Skipping character extraction: no model assigned for Update Memory.")
            return

        self.step_started.emit("Checking for new characters to track...")
        try:
            existing_names = ", ".join(c.name for c in self.project.characters) or "(none yet)"
            language = self._response_language()
            language_note = (
                f' The "description" value should be written in {language}.'
                if language else ""
            )
            # Large chunk size so a typical chapter/outline fits in one call.
            CHUNK_SIZE = 16000
            text_chunks = [
                source_text[i:i + CHUNK_SIZE]
                for i in range(0, max(1, len(source_text)), CHUNK_SIZE)
            ]
            total_added = 0
            for chunk_idx, chunk in enumerate(text_chunks):
                if self._cancelled:
                    break
                if chunk_idx > 0:
                    # Refresh so later chunks don't duplicate characters found in earlier ones.
                    existing_names = ", ".join(c.name for c in self.project.characters) or "(none yet)"
                prompt = prompts.render(
                    "characters/extract_user",
                    language_note=language_note,
                    existing_names=existing_names,
                    chunk=chunk,
                )
                messages = [
                    {
                        "role": "system",
                        "content": prompts.load_raw("characters/extract_system"),
                    },
                    {"role": "user", "content": prompt},
                ]

                # Cap prompt+reply against n_ctx (bypasses build_context_for_model).
                context_limit = self._model_context_limit()
                prompt_tokens = estimate_messages_tokens(messages)
                effective_max_tokens = min(1500, max(64, context_limit - prompt_tokens - 32))

                engine = get_engine()
                self._log_prompt_if_enabled(f"characters chunk {chunk_idx + 1}/{len(text_chunks)}", messages)
                try:
                    with _StepTimer(f"generate(characters chunk {chunk_idx + 1}/{len(text_chunks)})"):
                        raw = engine.generate(
                            messages=messages,
                            max_tokens=effective_max_tokens,
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
        finally:
            logger.info(f"[timing] extract_and_merge_characters (total): {time.monotonic() - _t_start:.2f}s")

    def _merge_extracted_characters(self, raw_json: str) -> int:
        """
Merge model-extracted character candidates into project.characters.
        Uses character_dedup to avoid duplicates; matched candidates enrich existing records."""
        text = raw_json.strip()
        # Strip markdown code fences some models add around JSON.
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

        added = 0
        for entry in data:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name", "")).strip()
            if not name:
                continue

            existing, score = find_existing_character(entry, self.project.characters)
            if existing is not None:
                merge_nonempty_fields(existing, entry)
                logger.info(
                    "Character extraction: reused existing character %r for "
                    "candidate %r (identity score=%.2f).",
                    existing.name, name, score,
                )
                continue

            role = str(entry.get("role", "supporting")).strip().lower()
            if role not in ("protagonist", "antagonist", "supporting", "minor"):
                role = "supporting"
            description = str(entry.get("description", "")).strip()
            backstory = str(entry.get("backstory", "") or "").strip()
            traits_raw = entry.get("traits", [])
            if isinstance(traits_raw, str):
                traits = [t.strip() for t in traits_raw.split(",") if t.strip()]
            elif isinstance(traits_raw, list):
                traits = [str(t).strip() for t in traits_raw if str(t).strip()]
            else:
                traits = []
            self.project.characters.append(
                Character(
                    name=name,
                    role=role,
                    description=description,
                    backstory=backstory,
                    traits=traits,
                )
            )
            added += 1
            logger.info("Character extraction: added new character %r.", name)
        return added

    def _update_world_incremental(self, source_text: str, source_type: str = "chapter") -> None:
        """
Extract stable world facts from generated text and merge them into World & Setting.
        Do NOT call on synopsis text (synopsis is plot summary, not worldbuilding).
        New facts are returned as Markdown bullets and merged structurally — no SEARCH/REPLACE."""
        if not source_text or not source_text.strip():
            return
        _t_start = time.monotonic()
        if not self._load_model_for_task(TaskType.GENERATE_WORLD):
            logger.warning("Skipping world update: no model assigned for World generation.")
            return

        self.step_started.emit("Checking for new world details...")
        try:
            language = self._response_language()
            language_note = f" Write in {language}." if language else ""

            self.project.world = _wf_ensure_sections(self.project.world, DEFAULT_WORLD_SECTIONS)

            system_content = prompts.render(
                "world/incremental_system",
                language_note=language_note,
            )

            CHUNK_SIZE = 16000
            text_chunks = [
                source_text[i:i + CHUNK_SIZE]
                for i in range(0, max(1, len(source_text)), CHUNK_SIZE)
            ]
            for chunk_idx, chunk in enumerate(text_chunks):
                if self._cancelled:
                    break

                with _StepTimer(f"world_update chunk {chunk_idx + 1}/{len(text_chunks)}"):
                    existing_world = self.project.world.strip() or "(none yet)"

                    user_content = prompts.render(
                        "world/incremental_user",
                        existing_world=existing_world,
                        chapter_chunk=chunk,
                    )

                    raw = self._run_lean_inference(
                        TaskType.GENERATE_WORLD, system_content, user_content, max_tokens=500,
                    )
                    if not raw:
                        continue
                    cleaned = raw.strip()
                    if not cleaned or "NO_NEW_WORLD_DETAILS" in cleaned.upper():
                        logger.info(
                            f"[world_update] chunk {chunk_idx + 1}: no new world details."
                        )
                        continue

                    # Merge by section heading; duplicate lines are silently skipped.
                    updated = _wf_merge_markdown_document(
                        self.project.world, cleaned, DEFAULT_WORLD_SECTIONS
                    )
                    self.project.world = updated
                    logger.info(
                        f"[world_update] chunk {chunk_idx + 1}/{len(text_chunks)}: "
                        "merged new world facts."
                    )
        finally:
            logger.info(f"[timing] update_world_incremental (total): {time.monotonic() - _t_start:.2f}s")

    def _run_inference(
        self,
        task: TaskType,
        user_message: str,
        add_to_chat: bool = True,
        max_tokens: int = 2048,
    ) -> str:
        return self._run_inference_v2(task, user_message, add_to_chat=add_to_chat, max_tokens=max_tokens)

    def _run_lean_inference(
        self,
        task: TaskType,
        system_content: str,
        user_content: str,
        max_tokens: int = 500,
    ) -> str:
        """
Minimal-context inference: sends only the caller-built system/user pair,
        without Synopsis, Outline, Characters, Memory, or chat history."""
        if self._cancelled:
            logger.info(f"[{task.value}] lean inference cancelled before start.")
            return ""
        if not self._load_model_for_task(task):
            return ""

        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content},
        ]
        context_limit = self._model_context_limit()
        prompt_tokens = estimate_messages_tokens(messages)
        effective_max_tokens = min(max_tokens, max(0, context_limit - prompt_tokens))
        if effective_max_tokens <= 0:
            logger.warning(
                f"[{task.value}] lean inference: prompt ({prompt_tokens} tokens) "
                f"leaves no room for a reply within context_limit={context_limit}."
            )
            return ""

        temperature = self._task_temperature(task)
        top_p = self._task_top_p(task)
        top_k = self._task_top_k(task)
        logger.info(
            f"[{task.value}] lean inference: prompt_tokens={prompt_tokens}, "
            f"max_tokens={effective_max_tokens}, temperature={temperature}, "
            f"top_p={top_p}, top_k={top_k}"
        )

        engine = get_engine()
        self._log_prompt_if_enabled(f"lean:{task.value}", messages)
        try:
            with _StepTimer(f"lean_generate({task.value})"):
                result = engine.generate(
                    messages=messages,
                    max_tokens=effective_max_tokens,
                    temperature=temperature,
                    top_p=top_p,
                    top_k=top_k,
                    stream=True,
                    stream_callback=lambda _t: None,
                    cancel_check=lambda: self._cancelled,
                )
        except Exception as e:
            logger.warning(f"[{task.value}] lean inference failed: {e}")
            return ""
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
        with _StepTimer(f"dispatch({task.value})"):
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
            elif task == TaskType.CHANGE_CHAPTER:
                self._run_change_chapter()
            elif task == TaskType.UPDATE_MEMORY:
                self._run_update_memory()
            elif task == TaskType.CONVERSATION_SUMMARY:
                self._run_conversation_summary()

    # ──────────────────────────────────────────────
    # Task implementations
    # ──────────────────────────────────────────────

    def _detect_chapter_continuation_request(self, message: str) -> Optional[int]:
        """
Detect continuation requests ("continue chapter 3", "sigue escribiendo", etc.).
        Returns the target chapter number, or None if not a continuation request."""
        if not self.project.chapters:
            return None

        lower = message.lower()
        # Strip accents for accent-insensitive matching.
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


        m = re.search(r"(?:cap[ií]tulo|chapter)\s*(\d+)", lower)
        if m:
            num = int(m.group(1))
            if any(c.number == num for c in self.project.chapters):
                return num
            return None  # they named a chapter that doesn't exist — don't guess

        # Without an explicit chapter number, require a story-related word or a short message.
        mentions_story = any(
            w in normalized for w in ("cap", "chapter", "historia", "story", "escena", "scene")
        )
        if mentions_story or len(message.strip()) <= 40:
            return max(c.number for c in self.project.chapters)

        return None

    def _run_chat_continue_chapter(self, chapter_num: int) -> None:
        chapter = next((c for c in self.project.chapters if c.number == chapter_num), None)
        if not chapter:
            # Shouldn't happen, but fall back to a normal chat reply.
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

        # add_to_chat=False: show the author's original message in chat, not the constructed prompt.
        result = self._run_inference(
            TaskType.WRITE_CHAPTER, model_prompt, add_to_chat=False, max_tokens=self._content_max_tokens()
        )
        if result:
            sep = "\n\n" if chapter.content.strip() else ""
            chapter.content = (chapter.content.rstrip() + sep + result).strip()
            chapter.reviewed = False

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
            self._update_world_incremental(result, source_type="chapter")
            storage.save_project(self.project)
            self.step_finished.emit(f"Continued Chapter {chapter_num}", result)

    def _run_chat(self) -> None:
        self.step_started.emit("Generating response...")

        # Chapter attachment: parse and dispatch before normal chat handling.
        if self.extra_input.startswith(CHAT_CHAPTER_ATTACHMENT_MARKER):
            self._run_chat_with_chapter_attachment()
            return

        target_chapter_num = self._detect_chapter_continuation_request(self.extra_input)
        if target_chapter_num is not None:
            self._run_chat_continue_chapter(target_chapter_num)
            return

        result = self._run_inference(TaskType.CHAT, self.extra_input, add_to_chat=True)
        if result:
            self._maybe_summarize()
            storage.save_project(self.project)
            self.step_finished.emit("Chat", result)

    def _run_chat_with_chapter_attachment(self) -> None:
        """
Handle a chat message with a chapter attachment (CHAT_CHAPTER_ATTACHMENT_MARKER format)."""
        body = self.extra_input[len(CHAT_CHAPTER_ATTACHMENT_MARKER):].lstrip("\n")
        try:
            header, rest = body.split("===ATTACHMENT_CONTENT===\n", 1)
            content, user_request = rest.split("\n===ATTACHMENT_END===\n", 1)
        except ValueError:
            logger.warning("Malformed chat chapter attachment; falling back to plain chat.")
            result = self._run_inference(TaskType.CHAT, self.extra_input, add_to_chat=True)
            if result:
                self._maybe_summarize()
                storage.save_project(self.project)
                self.step_finished.emit("Chat", result)
            return

        header_lines = header.split("\n", 1)
        chapter_num = header_lines[0].strip()
        chapter_title = header_lines[1].strip() if len(header_lines) > 1 else ""

        prompt = prompts.render(
            "chat/chapter_attachment",
            chapter_number=chapter_num,
            chapter_title=chapter_title,
            chapter_content=content,
            user_request=user_request.strip(),
        )

        self.step_started.emit("Reading inserted chapter context...")
        result = self._run_inference(TaskType.CHAT, prompt, add_to_chat=False)
        if not result:
            return

        # Save only the short human-visible message, not the full inserted chapter text.
        self.project.chat_messages.append(ChatMessage(
            role=MessageRole.USER,
            content=f"[Chapter {chapter_num} attached] {user_request.strip()}",
        ))
        self.project.chat_messages.append(ChatMessage(role=MessageRole.ASSISTANT, content=result))
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
            # Synopsis is plot summary, not worldbuilding — skip world extraction.
            storage.save_project(self.project)
            self.step_finished.emit("Synopsis", result)

    def _run_generate_outline(self) -> None:
        """
Generate the full outline, continuing until the requested chapter count is reached.
        Completion is checked deterministically: does "## Chapter N" exist in the text?"""
        self.step_started.emit("Generating outline...")
        if self.extra_input.startswith(OUTLINE_EXTEND_MARKER):
            self._run_extend_outline()
            return
        if self.extra_input.startswith(OUTLINE_SUGGESTION_MARKER):
            self._run_regenerate_outline_with_suggestion()
            return
        # extra_input carries the chapter-count and optional author notes from the Generate dialog.
        base_prompt = (
            f"Generate a complete chapter-by-chapter outline for '{self.project.title}'.\n"
            "Use the structured format with Objective, Story Progression, and Continuity for each chapter."
        )
        if self.extra_input:
            base_prompt += f"\n\n{self.extra_input}"
        if self.project.synopsis:
            base_prompt += f"\n\nSynopsis: {self.project.synopsis}"

        requested_n = self._extract_requested_chapter_count(self.extra_input)

        # Pre-render the outline template with requested_count, injected into the prompts
        # cache so build_system_prompt picks it up; restored after the loop.
        _outline_template_key = "task_instructions/generate_outline"
        _requested_count_str = str(requested_n) if requested_n is not None else "(not specified)"
        _original_cached = prompts._cache.get(_outline_template_key)
        try:
            prompts._cache[_outline_template_key] = prompts.render(
                _outline_template_key,
                requested_count=_requested_count_str,
            )
        except Exception:
            # If render fails for any reason, fall back to the raw template
            # so the outline generation still works.
            pass

        try:
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

                # Strip repeated chapters from continuation passes.
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

                # Keep project.outline in sync so the next pass sees what's already written.
                self.project.outline = outline_text

                if not requested_n:
                    # No target chapter count — one pass only.
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
                self._update_world_incremental(outline_text, source_type="outline")

                # Warn if the chapter count doesn't match the request.
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
        finally:
            # Restore the prompts cache so other callers get the raw template.
            if _original_cached is None:
                prompts._cache.pop(_outline_template_key, None)
            else:
                prompts._cache[_outline_template_key] = _original_cached

    def _run_regenerate_outline_with_suggestion(self) -> None:
        """
Apply one targeted suggestion to the existing outline without full regeneration."""
        suggestion = self.extra_input.split(OUTLINE_SUGGESTION_MARKER, 1)[1].strip()
        if not suggestion:
            self.error_occurred.emit("No outline suggestion was provided.")
            return

        current_outline = self.project.outline.strip()
        if not current_outline:
            self.error_occurred.emit("There is no outline to regenerate. Generate an outline first.")
            return

        self.step_started.emit("Regenerating outline from your suggestion...")

        prompt = prompts.render(
            "outline/regenerate_with_suggestion",
            title=self.project.title,
            suggestion=suggestion,
            current_outline=current_outline,
        )

        result = self._run_inference(
            TaskType.GENERATE_OUTLINE,
            prompt,
            add_to_chat=False,
            max_tokens=self._content_max_tokens(),
        )
        if not result:
            return

        updated = result.strip()
        self.project.outline = updated
        self.project.chat_messages.append(ChatMessage(
            role=MessageRole.USER,
            content=f"Regenerate outline with suggestion: {suggestion}",
        ))
        self.project.chat_messages.append(ChatMessage(role=MessageRole.ASSISTANT, content=updated))

        self._extract_and_merge_characters(updated)
        self._update_world_incremental(updated, source_type="outline")
        storage.save_project(self.project)
        self.step_finished.emit("Outline Regenerated", updated)

    # ------------------------------------------------------------------
    # Extend Outline — Phase 1 helpers
    # ------------------------------------------------------------------

    def _parse_extend_plan(self, plan_text: str, next_chapter: int, requested_count: int) -> list[str]:
        """
Parse planning output ("Chapter N → description" lines) into a list of descriptions.
        Returns an empty list if parsing fails or count doesn't match."""
        descriptions: list[str] = []
        seen_chapters: set[int] = set()
        for line in plan_text.splitlines():
            line = line.strip()
            # Match "Chapter N →" or "Chapter N ->" (model may use ASCII arrow)
            m = re.match(r"(?i)chapter\s+(\d+)\s*(?:→|->|–>|—>)\s*(.*)", line)
            if not m:
                continue
            ch_num = int(m.group(1))
            desc = m.group(2).strip()
            if ch_num in seen_chapters:
                continue
            seen_chapters.add(ch_num)
            descriptions.append((ch_num, desc))

        descriptions.sort(key=lambda x: x[0])
        final_chapter = next_chapter + requested_count - 1
        valid = [
            desc for (ch_num, desc) in descriptions
            if next_chapter <= ch_num <= final_chapter
        ]
        if len(valid) != requested_count:
            return []
        return valid

    def _build_extend_outline_reference(
        self,
        current_outline: str,
        accumulated_entries: str,
        existing_tail_n: int,
        accumulated_tail_n: int,
    ) -> str:
        """Build the reference outline context for a single chapter generation pass."""
        existing_part = (
            self._tail_outline_chapters(current_outline, existing_tail_n)
            if existing_tail_n > 0 else ""
        )
        accumulated_part = (
            self._tail_outline_chapters(accumulated_entries, accumulated_tail_n)
            if accumulated_entries and accumulated_tail_n > 0 else ""
        )
        if existing_part and accumulated_part:
            return existing_part + "\n\n" + accumulated_part
        return existing_part or accumulated_part or "(none — this is the first chapter)"

    def _run_extend_outline(self) -> None:
        """
Two-phase Extend Outline: Phase 1 plans chapter distribution, Phase 2 generates each
        chapter independently via lean inference. project.outline is only updated after all
        chapters pass validation."""
        payload = self.extra_input.split(OUTLINE_EXTEND_MARKER, 1)[1].strip("\n")
        # Format: "<count>\n<request>" — count is a hard constraint, validated here.
        count_line, _, rest = payload.partition("\n")
        try:
            requested_count = int(count_line.strip())
        except ValueError:
            self.error_occurred.emit("No valid number of chapters to add was provided.")
            return
        if requested_count < 1:
            self.error_occurred.emit("The number of chapters to add must be at least 1.")
            return
        user_request = rest.strip()
        if not user_request:
            self.error_occurred.emit("No extension request was provided.")
            return

        current_outline = self.project.outline.strip()
        if not current_outline:
            self.error_occurred.emit("There is no outline to extend. Generate an outline first.")
            return

        next_chapter_start = self._outline_max_chapter_number() + 1
        final_chapter = next_chapter_start + requested_count - 1

        characters = format_characters_block(self.project.characters) or "(none)"
        world = self.project.world.strip() or "(none)"
        memory = self.project.memory.strip() or "(none)"
        intent_frag = self.project.author_intent.to_prompt_fragment() or "(none)"
        style_frag = self.project.writing_style.to_prompt_fragment() or "(none)"
        language = self._response_language()
        language_note = f" Write in {language}." if language else ""

        existing_numbers = set(self._outline_chapter_numbers())

        # ---- PHASE 1 — Planning ----
        self.step_started.emit(
            f"Planning {requested_count} new chapter(s) starting at Chapter {next_chapter_start}..."
        )
        logger.info(
            f"[extend_outline] Phase 1: planning {requested_count} chapters "
            f"({next_chapter_start}–{final_chapter})."
        )

        plan_system = prompts.render(
            "outline/extend_plan_system",
            language_note=language_note,
            requested_count=requested_count,
            next_chapter=next_chapter_start,
            last_new_chapter=final_chapter,
        )
        # Use only a tail of the existing outline to stay within context budget.
        plan_outline_ref = self._tail_outline_chapters(
            current_outline, EXTEND_OUTLINE_REFERENCE_TAIL_CHAPTERS
        ) or current_outline

        plan_user = prompts.render(
            "outline/extend_plan_user",
            title=self.project.title,
            user_request=user_request,
            requested_count=requested_count,
            next_chapter=next_chapter_start,
            last_new_chapter=final_chapter,
            characters=characters,
            world=world,
            memory=memory,
            author_intent=intent_frag,
            writing_style=style_frag,
            current_outline=plan_outline_ref,
        )

        # Planning output is one line per chapter; 512 tokens is sufficient.
        plan_raw = self._run_lean_inference(
            TaskType.GENERATE_OUTLINE, plan_system, plan_user, max_tokens=512,
        )
        if not plan_raw or not plan_raw.strip():
            self.error_occurred.emit(
                "Extend Outline: the planning phase returned no output. "
                "Nothing was appended — try again."
            )
            return

        chapter_plans = self._parse_extend_plan(plan_raw.strip(), next_chapter_start, requested_count)
        if not chapter_plans:
            self.error_occurred.emit(
                f"Extend Outline: could not parse a {requested_count}-chapter plan from the "
                "planning phase output. Nothing was appended — try again."
            )
            return

        logger.info(
            f"[extend_outline] Phase 1 complete: {len(chapter_plans)} chapter plans parsed."
        )

        # ---- PHASE 2 — Per-chapter generation (each chapter is an independent inference) ----
        chapter_system = prompts.render(
            "outline/extend_system",
            language_note=language_note,
        )

        reference_budgets = [
            (EXTEND_OUTLINE_REFERENCE_TAIL_CHAPTERS, EXTEND_OUTLINE_ACCUMULATED_TAIL_CHAPTERS),
            (1, 1),
            (1, 0),
            (0, 0),
        ]
        try:
            context_limit = self._model_context_limit()
        except Exception:
            context_limit = None
        min_reply_tokens = 300

        accumulated_entries = ""   # holds validated chapter text; never touches project.outline
        max_retries_per_chapter = MAX_OUTLINE_CONTINUATIONS  # retry budget per chapter

        for chapter_idx, chapter_plan_desc in enumerate(chapter_plans):
            next_chapter = next_chapter_start + chapter_idx
            self.step_started.emit(
                f"Generating Chapter {next_chapter} ({chapter_idx + 1}/{requested_count})..."
            )
            logger.info(
                f"[extend_outline] Phase 2, Chapter {next_chapter}: "
                f"plan='{chapter_plan_desc[:80]}...'"
            )

            # Each chapter receives its plan position (prev/next) without accumulated generated text.
            continuity_lines: list[str] = [
                "=== CHAPTER POSITION IN PLAN ===",
                f"Current chapter: Chapter {next_chapter}",
            ]
            if chapter_idx > 0:
                prev_ch = next_chapter - 1
                prev_plan = chapter_plans[chapter_idx - 1]
                continuity_lines.append(f"Previous planned chapter: Chapter {prev_ch} → {prev_plan}")
            if chapter_idx < requested_count - 1:
                nxt_ch = next_chapter + 1
                nxt_plan = chapter_plans[chapter_idx + 1]
                continuity_lines.append(f"Next planned chapter: Chapter {nxt_ch} → {nxt_plan}")
            continuity_context = "\n".join(continuity_lines)

            chapter_entry: str = ""
            for attempt in range(1, max_retries_per_chapter + 1):
                if self._cancelled:
                    return

                user_content = None
                for budget_idx, (existing_tail_n, accumulated_tail_n) in enumerate(reference_budgets):
                    candidate_reference = self._build_extend_outline_reference(
                        current_outline, accumulated_entries, existing_tail_n, accumulated_tail_n
                    )
                    candidate_user_content = prompts.render(
                        "outline/extend_chapter_user",
                        title=self.project.title,
                        user_request=user_request,
                        next_chapter=next_chapter,
                        chapter_plan=chapter_plan_desc,
                        continuity_context=continuity_context,
                        characters=characters,
                        world=world,
                        memory=memory,
                        author_intent=intent_frag,
                        writing_style=style_frag,
                        current_outline=candidate_reference,
                    )
                    is_last_budget = budget_idx == len(reference_budgets) - 1
                    if context_limit is None or is_last_budget:
                        user_content = candidate_user_content
                        break
                    candidate_messages = [
                        {"role": "system", "content": chapter_system},
                        {"role": "user", "content": candidate_user_content},
                    ]
                    prompt_tokens = estimate_messages_tokens(candidate_messages)
                    if context_limit - prompt_tokens >= min_reply_tokens:
                        user_content = candidate_user_content
                        break

                result = self._run_lean_inference(
                    TaskType.GENERATE_OUTLINE,
                    chapter_system,
                    user_content,
                    max_tokens=self._content_max_tokens(),
                )
                if not result or not result.strip():
                    logger.warning(
                        f"[extend_outline] Chapter {next_chapter} attempt {attempt}: "
                        "returned no text — retrying."
                    )
                    continue

                candidate = self._normalize_chapter_headings(result.strip())
                candidate = self._strip_preamble_before_heading(candidate)
                candidate = self._extract_new_outline_chapters(candidate, next_chapter - 1)
                if not candidate:
                    logger.warning(
                        f"[extend_outline] Chapter {next_chapter} attempt {attempt}: "
                        "no valid chapter found after stripping — retrying."
                    )
                    continue

                candidate = self._drop_incomplete_trailing_chapter(candidate)
                if not candidate:
                    logger.warning(
                        f"[extend_outline] Chapter {next_chapter} attempt {attempt}: "
                        "chapter appears truncated — retrying."
                    )
                    continue

                pass_numbers = self._chapter_numbers_in_text(candidate)
                if not pass_numbers or pass_numbers[0] != next_chapter:
                    logger.warning(
                        f"[extend_outline] Chapter {next_chapter} attempt {attempt}: "
                        f"got chapter number(s) {pass_numbers} instead of {next_chapter} — retrying."
                    )
                    continue

                # Trim any extra chapters the model appended beyond this one.
                if pass_numbers[-1] > next_chapter:
                    m = re.search(
                        rf"(?m)^\s*##\s*Chapter\s+{next_chapter + 1}\b", candidate
                    )
                    if m:
                        candidate = candidate[:m.start()].strip()
                    pass_numbers = self._chapter_numbers_in_text(candidate)
                    if not pass_numbers:
                        continue

                if existing_numbers & set(pass_numbers):
                    collision = sorted(existing_numbers & set(pass_numbers))
                    logger.warning(
                        f"[extend_outline] Chapter {next_chapter} attempt {attempt}: "
                        f"collides with existing chapter(s) {collision} — retrying."
                    )
                    continue

                chapter_entry = candidate
                logger.info(
                    f"[extend_outline] Chapter {next_chapter} generated "
                    f"(attempt {attempt})."
                )
                break

            if not chapter_entry:
                self.error_occurred.emit(
                    f"Extend Outline could not generate Chapter {next_chapter} after "
                    f"{max_retries_per_chapter} attempt(s). Nothing was appended — try again."
                )
                return

            # Hold in memory; project.outline is not updated until all chapters are validated.
            accumulated_entries = (
                (accumulated_entries.rstrip() + "\n\n" + chapter_entry.strip()).strip()
                if accumulated_entries else chapter_entry.strip()
            )

        # ---- All chapters generated — validate and save atomically ----
        new_entries = accumulated_entries
        new_numbers = self._chapter_numbers_in_text(new_entries)

        # Final validation on the full accumulated output.
        if not new_numbers:
            self.error_occurred.emit("The model returned no new chapters.")
            return
        if new_numbers[0] != next_chapter_start:
            self.error_occurred.emit(
                f"Extend Outline expected the new chapters to start at Chapter "
                f"{next_chapter_start}, but they started at Chapter {new_numbers[0]}. "
                "Nothing was appended — try again."
            )
            return
        duplicates = existing_numbers & set(new_numbers)
        if duplicates:
            self.error_occurred.emit(
                f"The model repeated existing chapter number(s) {sorted(duplicates)}. "
                "Nothing was appended — try again."
            )
            return
        if new_numbers != list(range(next_chapter_start, next_chapter_start + len(new_numbers))):
            self.error_occurred.emit(
                "The new chapters weren't numbered sequentially with no gaps or repeats. "
                "Nothing was appended — try again."
            )
            return
        if len(new_numbers) != requested_count:
            self.error_occurred.emit(
                f"You asked for exactly {requested_count} new chapter(s), but "
                f"{len(new_numbers)} were accumulated. Nothing was appended — try again."
            )
            return
        if new_numbers[-1] != final_chapter:
            self.error_occurred.emit(
                f"Expected the new chapters to end at Chapter {final_chapter}, but they "
                f"ended at Chapter {new_numbers[-1]}. Nothing was appended — try again."
            )
            return

        self.project.outline = current_outline + "\n\n" + new_entries
        self.project.chat_messages.append(ChatMessage(
            role=MessageRole.USER,
            content=f"Extend outline: {user_request}",
        ))
        self.project.chat_messages.append(ChatMessage(role=MessageRole.ASSISTANT, content=new_entries))

        self._extract_and_merge_characters(new_entries)
        self._update_world_incremental(new_entries, source_type="outline")
        storage.save_project(self.project)
        self.step_finished.emit(f"Outline Extended (Chapters {new_numbers[0]}-{new_numbers[-1]})", new_entries)

    def _extract_requested_chapter_count(self, text: str) -> Optional[int]:
        """Extract the requested chapter count from the Generate dialog's extra_input text."""
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
    def _drop_incomplete_trailing_chapter(text: str) -> str:
        """
Drop the last "## Chapter N" block if it appears truncated (missing "Continuity:" or
        ending without terminal punctuation). Earlier blocks are always complete by construction."""
        if not text.strip():
            return text
        matches = list(re.finditer(r"(?m)^\s*##\s*Chapter\s+\d+\b.*$", text))
        if not matches:
            return text
        last_start = matches[-1].start()
        last_block = text[last_start:].rstrip()

        has_continuity = re.search(r"(?mi)^\s*Continuity:\s*", last_block) is not None
        ends_cleanly = bool(re.search(r'[.!?"\')\]]\s*$', last_block))

        if has_continuity and ends_cleanly:
            return text.rstrip()

        # Incomplete — discard the last block.
        return text[:last_start].rstrip()

    @staticmethod
    def _extract_new_outline_chapters(generated: str, highest_already: int) -> str:
        """
Strip any chapters <= highest_already from a continuation response.
        Returns the text starting from the first genuinely new chapter heading,
        or "" if no new chapters were found."""
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
                # Repeated or pre-chapter line — skip.
            else:
                new_lines.append(line)
        return "\n".join(new_lines).strip()

    def _build_outline_continuation_prompt(self, outline_so_far: str, requested_n: Optional[int]) -> str:
        """
Build the continuation prompt for generate-outline passes 2+.
        Embeds the full accumulated outline in the user message so the model
        doesn't restart from Chapter 1 (the system prompt omits project.outline
        for GENERATE_OUTLINE to avoid contaminating fresh generations)."""
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
            "Objective, Story Progression, Continuity.",
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
            TaskType.GENERATE_WORLD, prompt, add_to_chat=True, max_tokens=800
        )
        if result:
            # Merge by section heading into the existing document.
            self.project.world = _wf_merge_markdown_document(
                self.project.world, result, DEFAULT_WORLD_SECTIONS
            )
            self._extract_and_merge_characters(result)
            storage.save_project(self.project)
            self.step_finished.emit("World & Setting", result)

    def _extract_chapter_outline_section(self, chapter_number: int) -> str:
        """
Return this chapter's outline entry ("## Chapter N: Title" block), or "" if not found."""
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
    def _strip_preamble_before_heading(text: str) -> str:
        """
Discard any model preamble before the first "## Chapter N" heading."""
        if not text:
            return text
        match = re.search(r"(?m)^\s*##\s*Chapter\s+\d+\b", text, re.IGNORECASE)
        if not match:
            return text
        return text[match.start():]

    @staticmethod
    def _normalize_chapter_headings(text: str) -> str:
        """
Normalise any near-miss chapter heading style into the canonical "## Chapter N: Title" form.
        Extend Outline one-shot calls often drift to bare/bold/wrongly-leveled headings;
        this fixes them before validation runs rather than rejecting correct content."""
        if not text:
            return text

        # Strip code fences some models wrap the entire response in.
        fenced = re.match(r"^\s*```(?:[a-zA-Z]*)\n(.*)\n```\s*$", text, re.DOTALL)
        if fenced:
            text = fenced.group(1)

        heading_re = re.compile(
            r"(?im)^[ \t]*#{0,6}[ \t]*\*{0,2}[ \t]*chapter[ \t]+(\d+)"
            r"[ \t]*:?[ \t]*\*{0,2}[ \t]*(.*)$"
        )

        def _canonicalize(match: "re.Match") -> str:
            number, title = match.group(1), match.group(2).strip(" *")
            return f"## Chapter {number}: {title}".rstrip(": ")

        return heading_re.sub(_canonicalize, text)

    def _tail_outline_chapters(self, outline_text: str, keep_last_n: int) -> str:
        """
Return the last keep_last_n chapter entries from outline_text.
        Keeps Extend Outline passes from re-sending the full existing outline on each iteration."""
        if keep_last_n <= 0 or not outline_text:
            return outline_text
        numbers = self._chapter_numbers_in_text(outline_text)
        if len(numbers) <= keep_last_n:
            return outline_text
        cutoff_chapter = numbers[-keep_last_n]
        matches = list(re.finditer(
            rf"(?m)^\s*##\s*Chapter\s+{cutoff_chapter}\b", outline_text
        ))
        if not matches:
            return outline_text
        return outline_text[matches[0].start():].strip()

    @staticmethod
    def _chapter_numbers_in_text(text: str) -> list[int]:
        """
Return sorted chapter numbers from "## Chapter N" headings in text."""
        numbers = set()
        for match in re.finditer(r"^\s*##\s*Chapter\s+(\d+)\b", text or "", re.IGNORECASE | re.MULTILINE):
            try:
                numbers.add(int(match.group(1)))
            except ValueError:
                continue
        return sorted(numbers)

    def _outline_chapter_title(self, chapter_number: int) -> str:
        """
Return the chapter title from its "## Chapter N: Title" heading, or "" if not found."""
        section = self._extract_chapter_outline_section(chapter_number)
        if not section:
            return ""
        first_line = section.split("\n", 1)[0]
        match = re.match(r"(?i)^\s*##\s*Chapter\s+\d+\s*:?\s*(.*)$", first_line)
        if not match:
            return ""
        return match.group(1).strip(" *")

    def _outline_chapter_numbers(self) -> list[int]:
        return self._chapter_numbers_in_text(self.project.outline or "")

    def _outline_max_chapter_number(self) -> int:
        numbers = self._outline_chapter_numbers()
        return max(numbers, default=0)

    def _outline_has_chapter(self, chapter_number: int) -> bool:
        return chapter_number in set(self._outline_chapter_numbers())

    def _next_chapter_number(self) -> int:
        """
Return the next chapter number to write, based on the highest chapter number saved."""
        highest_existing = max((c.number for c in self.project.chapters), default=0)
        return max(self.project.current_chapter, highest_existing) + 1

    def _clear_temporary_chat_history(self) -> None:
        self.project.chat_messages = []
        storage.save_project(self.project)

    def _clear_chat_messages_for_continuation(self) -> None:
        """
Clear transient chat history between continuation passes."""
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
        return prompts.render(
            "write_chapter/completion_eval_user",
            chapter_number=chapter_num,
            title=self.project.title,
            chapter_goal=chapter_goal.strip() or "(none)",
            outline_block=f"\nChapter {chapter_num} Outline Entry:\n{outline_context}\n" if outline_context else "",
            chapter_text=chapter_text,
        )

    def _build_chapter_continuation_prompt(
        self,
        chapter_num: int,
        chapter_text: str,
        chapter_goal: str,
        checklist: str = "",
        missing: "list[str] | None" = None,
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
                f"\nChapters already fully written and saved: {', '.join(str(n) for n in completed)}. "
                f"Do NOT rewrite any of them.\n"
            )
        else:
            completed_note = ""

        checklist_block = ""
        if checklist:
            missing_text = "\n".join(f"- {item}" for item in (missing or [])) or \
                "(none identified; re-check every checklist item yourself)"
            checklist_block = "\n" + prompts.render(
                "change_chapter/section",
                heading="INTERNAL REQUIREMENTS CHECKLIST — STILL-MISSING ITEMS MUST BE ADDRESSED FIRST",
                body=f"Full checklist (internal — never expose to the reader):\n{checklist}\n\nStill missing:\n{missing_text}",
            )

        return prompts.render(
            "write_chapter/continuation_user",
            chapter_number=chapter_num,
            title=self.project.title,
            completed_note=completed_note,
            chapter_goal=chapter_goal.strip() or "(none)",
            checklist_block=checklist_block,
            chapter_tail=tail,
        )

    def _parse_completion_evaluation(self, text: str) -> dict:
        """
        Robust true/false parser. Accepts any response that unambiguously
        contains 'true' or 'false' — handles trailing punctuation, leading
        spaces, markdown backticks, and sentence-cased output that some models
        emit despite the prompt instructions.

        Returns {"valid": True/False, "completed": True/False, "raw": text}.
        "valid" is True whenever the response is unambiguous. It is False only
        when the response contains neither keyword or contains both (contradictory).
        """
        raw = text.strip()
        normalized = raw.lower()
        # Strip common noise characters the model may wrap the token in
        cleaned = normalized.strip("` \t\n.,;:\"'")
        has_true  = "true"  in cleaned
        has_false = "false" in cleaned
        if has_true and not has_false:
            return {"valid": True, "completed": True,  "raw": raw}
        if has_false and not has_true:
            return {"valid": True, "completed": False, "raw": raw}
        # Ambiguous or empty
        return {"valid": False, "completed": False, "raw": raw}

    def _evaluate_chapter_completion(
        self,
        chapter_num: int,
        chapter_text: str,
        chapter_goal: str,
        generation_pass: int = 0,
    ) -> dict:
        """
Ask the model whether the chapter draft is complete. Logs each attempt."""
        word_count = len(chapter_text.split())
        logger.info(
            "[eval] ── Chapter %d completion check (after pass %d) ──  "
            "%d words written so far",
            chapter_num, generation_pass, word_count,
        )

        system_content = prompts.load_raw("write_chapter/completion_eval_system")
        user_content = self._build_chapter_evaluation_prompt(chapter_num, chapter_text, chapter_goal)

        final = {"valid": False, "completed": False, "raw": ""}
        for attempt in range(1, MAX_COMPLETION_EVAL_RETRIES + 1):
            logger.info("[eval] Attempt %d/%d — sending to model…", attempt, MAX_COMPLETION_EVAL_RETRIES)
            raw_output = self._run_lean_inference(
                TaskType.REVIEW_CHAPTER,
                system_content,
                user_content,
                max_tokens=16,   # slightly more room so noise doesn't eat the token
            )
            parsed = self._parse_completion_evaluation(raw_output)
            verdict = "TRUE ✓" if parsed["completed"] else "FALSE →"
            if parsed["valid"]:
                logger.info(
                    "[eval] Attempt %d: model said %r → verdict: %s",
                    attempt, raw_output.strip(), verdict,
                )
                final = parsed
                break
            else:
                logger.warning(
                    "[eval] Attempt %d: model returned unparseable output: %r  "
                    "(expected 'true' or 'false')",
                    attempt, raw_output.strip(),
                )
                final = parsed  # keep last attempt's raw for the log below

        if not final["valid"]:
            # All retries failed — treat as not complete rather than truncating silently.
            logger.warning(
                "[eval] All %d attempts failed to get a clean true/false. "
                "Last raw output: %r  — treating as FALSE (will continue).",
                MAX_COMPLETION_EVAL_RETRIES, final.get("raw", ""),
            )
            final["completed"] = False

        action = "STOP — chapter complete." if final["completed"] else "CONTINUE — more text needed."
        logger.info("[eval] ── Result for Chapter %d: %s ──", chapter_num, action)
        return final

    def _build_chapter_generation_prompt(self, chapter_num: int, outline_entry: str, checklist: str) -> str:
        """
Build the WRITE_CHAPTER first-draft user prompt by assembling applicable context sections."""
        sections = []

        if outline_entry:
            sections.append(prompts.render(
                "change_chapter/section", heading="CHAPTER OUTLINE (binding)", body=outline_entry,
            ))

        if checklist:
            sections.append(prompts.render(
                "change_chapter/section",
                heading="INTERNAL REQUIREMENTS CHECKLIST (write ordinary prose — never expose this list to the reader)",
                body=checklist,
            ))

        if chapter_num > 1:
            prev = next((c for c in self.project.chapters if c.number == chapter_num - 1), None)
            if prev and prev.content:
                tail = prev.content[-800:].strip()
                sections.append(prompts.render(
                    "change_chapter/section", heading="END OF PREVIOUS CHAPTER (continuity only)", body=tail,
                ))

        # Reinforce style/intent at the point of generation.
        style_frag = self.project.writing_style.to_prompt_fragment()
        if style_frag:
            sections.append(prompts.render("change_chapter/section", heading="STYLE TO APPLY", body=style_frag))

        intent = self.project.author_intent
        intent_lines = []
        if intent.emotional_journey:
            intent_lines.append(f"Emotional tone to sustain in this chapter: {intent.emotional_journey}")
        if intent.avoid:
            intent_lines.append(f"Avoid entirely: {intent.avoid}")
        if intent_lines:
            sections.append(prompts.render("change_chapter/section", heading="AUTHOR INTENT", body="\n".join(intent_lines)))

        # extra_input supplements the structured context.
        if self.extra_input and self.extra_input.strip():
            sections.append(prompts.render("change_chapter/section", heading="AUTHOR'S REQUEST", body=self.extra_input.strip()))

        return prompts.render(
            "write_chapter/generation_user",
            chapter_number=chapter_num,
            title=self.project.title,
            sections="\n\n".join(sections),
        )

    def _run_write_chapter(self) -> None:
        # Derive chapter number from disk, not project.current_chapter (may be stale).
        chapter_num = self._next_chapter_number()
        self.step_started.emit(f"Writing Chapter {chapter_num}...")

        outline_entry = self._extract_chapter_outline_section(chapter_num)
        if not outline_entry and self.project.outline:
            # No matching "## Chapter N" heading — fall back to the full outline.
            outline_entry = self.project.outline

        # Derive a checklist from the outline entry (same planner/evaluate/continue
        # philosophy as CHANGE_CHAPTER). Skipped if there's no outline.
        checklist = ""
        if outline_entry:
            self.step_started.emit(f"Planning Chapter {chapter_num}...")
            checklist = change_chapter.plan_chapter(self, chapter_num, f"Chapter {chapter_num}", outline_entry)

        prompt = self._build_chapter_generation_prompt(chapter_num, outline_entry, checklist)

        # Fallback goal text for the true/false evaluator when no checklist is available.
        chapter_goal = outline_entry or prompt

        chapter_text = ""
        generation_pass = 0
        missing_items: list[str] = []
        evaluation = {"completed": True, "confidence": 100, "reason": "", "next": ""}

        while generation_pass < MAX_CONTINUATIONS:
            generation_pass += 1
            if generation_pass == 1:
                self.step_started.emit(f"Generation pass {generation_pass}...")
                logger.info(f"[write_chapter] Generation pass {generation_pass}...")
                generated = self._run_inference(
                    TaskType.WRITE_CHAPTER, prompt, add_to_chat=True, max_tokens=self._content_max_tokens()
                )
                if not generated:
                    logger.warning(f"[write_chapter] generation pass {generation_pass} returned empty text.")
                    break
                chapter_text = generated.strip()
            else:
                self.step_started.emit(f"Generating continuation (pass {generation_pass})...")
                logger.info(f"[write_chapter] Generating continuation (pass {generation_pass})...")
                # Clear chat history before building the next prompt to avoid
                # the model seeing the chapter tail twice.
                self._clear_chat_messages_for_continuation()
                continuation_prompt = self._build_chapter_continuation_prompt(
                    chapter_num,
                    chapter_text,
                    chapter_goal,
                    checklist,
                    missing_items,
                )
                # add_to_chat=False: the chapter tail is already embedded in the prompt.
                generated = self._run_inference(
                    TaskType.WRITE_CHAPTER, continuation_prompt, add_to_chat=False, max_tokens=self._content_max_tokens()
                )
                if not generated:
                    logger.warning(f"[write_chapter] generation pass {generation_pass} returned empty text.")
                    break

                # Trim overlap with existing text, same as CHANGE_CHAPTER continuations.
                addition = generated.strip()
                tail = chapter_text[-2500:].strip()
                addition = change_chapter.trim_leading_overlap(addition, tail)
                if change_chapter.is_substantial_duplicate(addition, tail):
                    logger.warning("[write_chapter] Continuation is a near-duplicate of existing prose; stopping.")
                    break
                if not addition:
                    logger.warning("[write_chapter] Continuation was empty after overlap trim.")
                    break
                chapter_text = (chapter_text.rstrip() + "\n\n" + addition).strip()

            if checklist:
                evaluation = change_chapter.evaluate_chapter(
                    self, chapter_num, f"Chapter {chapter_num}", chapter_text, outline_entry, checklist, generation_pass,
                )
                # Override: a truncated chapter is never "complete", even if the evaluator says so.
                if change_chapter.ends_abruptly(chapter_text) and evaluation["completed"]:
                    logger.warning(
                        f"[write_chapter] Pass {generation_pass}: evaluator said complete=true but "
                        "chapter ends abruptly — overriding."
                    )
                    evaluation["completed"] = False
                    if not any("truncat" in m.lower() for m in evaluation["missing"]):
                        evaluation["missing"].append("Chapter appears truncated mid-sentence.")
                missing_items = evaluation.get("missing", [])
            else:
                evaluation = self._evaluate_chapter_completion(
                    chapter_num,
                    chapter_text,
                    chapter_goal,
                    generation_pass=generation_pass,
                )
                missing_items = []

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
                # Use the outline heading as title source; fall back to generic title.
                outline_title = self._outline_chapter_title(chapter_num)
                ch = Chapter(
                    number=chapter_num,
                    title=outline_title or f"Chapter {chapter_num}",
                    content=chapter_text,
                )
                self.project.chapters.append(ch)

            self._extract_and_merge_characters(chapter_text)
            self._update_world_incremental(chapter_text, source_type="chapter")
            storage.save_project(self.project)
            self.step_finished.emit(f"Chapter {chapter_num}", chapter_text)

    def _run_write_book(self) -> None:
        # Start Write Book with a clean chat history.
        logger.info("[write_book] Clearing chat before Chapter 1.")
        self._clear_temporary_chat_history()
        self.clear_chat_requested.emit()
        self.step_started.emit("Clearing chat...")

        outline_numbers = self._outline_chapter_numbers()
        total = len(outline_numbers) if outline_numbers else max(1, len(self.project.chapters) + 1)
        max_outline_chapter = max(outline_numbers, default=0)
        self.step_started.emit(f"Writing Book (0/{total})...")
        written = 0
        while not self._cancelled:
            if not self._reload_project_from_storage():
                break
            # Refresh outline metadata after reload (it may have been edited).
            current_outline_numbers = self._outline_chapter_numbers()
            if current_outline_numbers:
                max_outline_chapter = max(current_outline_numbers)
                total = len(current_outline_numbers)
            total = total or max(1, len(self.project.chapters) + 1)
            pending = self._next_chapter_number()
            if pending <= 0:
                break
            if self.project.outline:
                # Stop if this chapter has no outline entry, or exceeds the highest outlined chapter.
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
            with _StepTimer(f"write_book chapter {pending}/{total} (total)"):
                self._run_write_chapter()
            written += 1

            if self._cancelled:
                break

            # Update Story Memory before the next chapter so it includes events from this one.
            wrote_chapter = any(c.number == pending for c in self.project.chapters)
            if wrote_chapter:
                # Point current_chapter at the finished chapter before calling _run_update_memory.
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

        # Include intent and style so the reviewer judges against the author's goals.
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
        """
Rewrite the current chapter based on review feedback via full regeneration."""
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

        self.step_started.emit(f"Rewriting Chapter {chapter_num} from review feedback...")

        language = self._response_language()
        language_note = f" Write in {language}." if language else ""
        style_frag = self.project.writing_style.to_prompt_fragment()

        system_content = prompts.render(
            "rewrite_chapter/full_rewrite_fallback_system",
            language_note=language_note,
            style_block=f"\n\nStyle to preserve:\n{style_frag}" if style_frag else "",
        )

        review_instruction = chapter.last_review
        if self.extra_input:
            review_instruction += f"\n\nAdditional author note: {self.extra_input}"

        user_content = prompts.render(
            "rewrite_chapter/full_rewrite_fallback_user",
            context_block="",
            chapter_number=chapter_num,
            chapter_title=chapter.title,
            chapter_content=chapter.content,
            instruction=review_instruction,
        )

        result = self._run_lean_inference(
            TaskType.REWRITE_CHAPTER, system_content, user_content,
            max_tokens=self._content_max_tokens(),
        )
        if not result or not result.strip():
            self.error_occurred.emit(
                "The model returned an empty rewrite. "
                "Try \"Rewrite with Feedback\" again."
            )
            return

        chapter.content = result.strip()
        chapter.reviewed = False
        chapter.last_review = ""
        self._extract_and_merge_characters(chapter.content)

        self.project.chat_messages.append(ChatMessage(
            role=MessageRole.USER,
            content=self.extra_input or f"Rewrite Chapter {chapter_num} with review feedback",
        ))
        self.project.chat_messages.append(ChatMessage(
            role=MessageRole.ASSISTANT,
            content=f"*(Rewrote Chapter {chapter_num} based on the review feedback.)*",
        ))

        storage.save_project(self.project)
        self.step_finished.emit(f"Rewrote Chapter {chapter_num}", chapter.content)

    def _finalize_changed_chapter(self, chapter_num: int, chapter: Chapter) -> None:
        """Refresh continuity data after a Change Chapter edit."""
        self._extract_and_merge_characters(chapter.content)
        self._update_world_incremental(chapter.content, source_type="chapter")
        self.project.current_chapter = chapter_num
        self._run_update_memory()

    def _run_change_chapter(self) -> None:
        """
        CHANGE_CHAPTER orchestration lives in engine.change_chapter — see
        that module for the full flow (continuity summary -> checklist plan
        -> full rewrite with clean context -> checklist verification ->
        continuation). This stays a thin delegator so _dispatch's mapping
        of TaskType -> handler is uniform across every workflow.
        """
        change_chapter.run(self)

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
        characters = format_characters_block(self.project.characters) or "(none)"
        chapter_content = chapter.content

        # Use _run_lean_inference to avoid injecting World/Characters/Memory twice
        # (they are already embedded manually below). Cap each section keeping the end,
        # since Memory cares about the chapter's conclusion more than its opening.
        WORLD_CAP, CHAR_CAP, MEMORY_CAP, CHAPTER_CAP = 2000, 1500, 2500, 14000
        if len(world) > WORLD_CAP:
            world = "[...]\n" + world[-WORLD_CAP:]
        if len(characters) > CHAR_CAP:
            characters = "[...]\n" + characters[-CHAR_CAP:]
        if len(existing_memory) > MEMORY_CAP:
            existing_memory = "[...]\n" + existing_memory[-MEMORY_CAP:]
        if len(chapter_content) > CHAPTER_CAP:
            chapter_content = "[... earlier part of the chapter omitted for length ...]\n\n" + chapter_content[-CHAPTER_CAP:]

        system_content = prompts.load_raw("memory/update_system")

        user_content = prompts.render(
            "memory/update_user",
            world=world,
            characters=characters,
            existing_memory=existing_memory,
            chapter_number=chapter_num,
            chapter_title=chapter.title,
            chapter_content=chapter_content,
        )

        result = self._run_lean_inference(
            TaskType.UPDATE_MEMORY, system_content, user_content, max_tokens=1500,
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

        system = prompts.load_raw("task_instructions/conversation_summary_system").strip()
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
                top_p=self._task_top_p(TaskType.CONVERSATION_SUMMARY),
                top_k=self._task_top_k(TaskType.CONVERSATION_SUMMARY),
                stream=True,
                stream_callback=on_token,
                cancel_check=lambda: self._cancelled,
            )
        except Exception as e:
            logger.error(f"[conversation_summary] error: {e}")
            self.error_occurred.emit(f"Summary error: {e}")
            return


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
        include_story_context: bool = True,
    ) -> None:
        super().__init__(parent)
        self.worker = WorkflowWorker(project, task, extra_input, settings,
                                     include_story_context=include_story_context)
        self.worker.moveToThread(self)


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


# ---------------------------------------------------------------------------
# Tests for _run_extend_outline() — run via `python -m engine.workflow`.
# ---------------------------------------------------------------------------

def _tb_chapter_block(n: int) -> str:
    return (
        f"## Chapter {n}: Title {n}\n\n"
        "Objective:\n"
        f"Objective for chapter {n}.\n\n"
        "Story Progression:\n"
        f"Story progression for chapter {n}.\n\n"
        "Continuity:\n"
        f"Continuity facts for chapter {n}."
    )


def _tb_make_plan(first: int, count: int) -> str:
    lines = []
    for i in range(count):
        n = first + i
        lines.append(f"Chapter {n} \u2192 Event for chapter {n}.")
    return "\n".join(lines)


def _tb_make_worker(existing_chapters: int, requested: int):
    from engine.models import Project
    existing_outline = "\n\n".join(_tb_chapter_block(n) for n in range(1, existing_chapters + 1))
    project = Project(title="Test Novel")
    project.outline = existing_outline
    worker = WorkflowWorker(
        project=project,
        task=TaskType.GENERATE_OUTLINE,
        extra_input=OUTLINE_EXTEND_MARKER + f"{requested}\nClara discovers the lie.",
    )
    return worker, existing_outline


def _test_phase1_produces_correct_number_of_plans() -> None:
    """Phase 1 must parse exactly N plan entries."""
    import unittest
    worker, _ = _tb_make_worker(8, 4)
    case = unittest.TestCase()

    plan_text = _tb_make_plan(9, 4)
    result = worker._parse_extend_plan(plan_text, 9, 4)
    case.assertEqual(len(result), 4, f"expected 4 plans, got {len(result)}")
    case.assertEqual(result[0], "Event for chapter 9.")
    case.assertEqual(result[3], "Event for chapter 12.")
    print("OK: Phase 1 parses exactly 4 chapter plans.")


def _test_phase2_makes_separate_inferences() -> None:
    """Total inferences = 1 (plan) + N (chapters) = N+1."""
    import unittest
    from unittest.mock import patch

    worker, _ = _tb_make_worker(8, 4)
    case = unittest.TestCase()

    responses = [_tb_make_plan(9, 4)] + [_tb_chapter_block(n) for n in range(9, 13)]

    with patch.object(worker, "_run_lean_inference", side_effect=responses) as mock_infer, \
         patch("engine.workflow.storage.save_project"), \
         patch.object(worker, "_extract_and_merge_characters"), \
         patch.object(worker, "_update_world_incremental"):

        errors = []
        worker.error_occurred.connect(lambda msg: errors.append(msg))
        worker._run_extend_outline()

    case.assertEqual(errors, [], f"unexpected error: {errors}")
    case.assertEqual(mock_infer.call_count, 5,
        f"expected 5 inferences (1 plan + 4 chapters), got {mock_infer.call_count}")
    print("OK: Phase 2 makes exactly 4 separate chapter inferences (5 total with plan).")


def _test_each_inference_receives_only_its_chapter_plan() -> None:
    """Each chapter prompt must contain its own plan line."""
    import unittest
    from unittest.mock import patch

    worker, _ = _tb_make_worker(8, 4)
    case = unittest.TestCase()

    all_responses = [_tb_make_plan(9, 4)] + [_tb_chapter_block(n) for n in range(9, 13)]
    captured: list[str] = []

    def _capture(task, system, user, max_tokens=500):
        captured.append(user)
        return all_responses.pop(0)

    with patch.object(worker, "_run_lean_inference", side_effect=_capture), \
         patch("engine.workflow.storage.save_project"), \
         patch.object(worker, "_extract_and_merge_characters"), \
         patch.object(worker, "_update_world_incremental"):

        errors = []
        worker.error_occurred.connect(lambda msg: errors.append(msg))
        worker._run_extend_outline()

    case.assertEqual(errors, [], f"unexpected error: {errors}")
    case.assertEqual(len(captured), 5)
    for i, n in enumerate(range(9, 13)):
        ch_prompt = captured[i + 1]
        case.assertIn(f"Event for chapter {n}.", ch_prompt,
                      f"Chapter {n} prompt missing its plan line")
    print("OK: Each chapter inference receives only its own plan line.")


def _test_temporary_history_cleared_between_chapters() -> None:
    """Each chapter must be an independent lean inference; verify no accumulated chat is forwarded."""
    import unittest
    from unittest.mock import patch

    worker, _ = _tb_make_worker(8, 4)
    case = unittest.TestCase()

    all_responses = [_tb_make_plan(9, 4)] + [_tb_chapter_block(n) for n in range(9, 13)]
    call_args_list: list[tuple] = []

    # Patch _run_lean_inference at engine.workflow level to capture args
    original = WorkflowWorker._run_lean_inference

    def _capture(self_inner, task, system, user, max_tokens=500):
        call_args_list.append((task, system, user))
        return all_responses.pop(0)

    with patch.object(WorkflowWorker, "_run_lean_inference", _capture), \
         patch("engine.workflow.storage.save_project"), \
         patch.object(worker, "_extract_and_merge_characters"), \
         patch.object(worker, "_update_world_incremental"):

        errors = []
        worker.error_occurred.connect(lambda msg: errors.append(msg))
        worker._run_extend_outline()

    case.assertEqual(errors, [], f"unexpected error: {errors}")
    # 5 total calls: 1 plan + 4 chapters
    case.assertEqual(len(call_args_list), 5)
    # Each chapter call (indices 1-4) must generate exactly ONE chapter number
    for i, n in enumerate(range(9, 13)):
        _, _, user_prompt = call_args_list[i + 1]
        # Prompt must target this chapter
        case.assertIn(f"Chapter {n}", user_prompt)
        # Prompt must NOT contain generation markers from other chapters
        # (i.e., it must not include the model's generated chapter blocks from
        # other chapters as a multi-turn assistant message — only the outline
        # tail reference context is allowed)
        for other_n in range(9, 13):
            if other_n != n and other_n < n:
                # Generated chapter text sent as assistant role would look like
                # two consecutive chapter entries in the user prompt without a
                # clear "EXISTING OUTLINE" label; verify the prompt doesn't
                # contain chapter content generated in this session as if it
                # were a prior assistant turn
                marker = f"Objective for chapter {other_n}."
                # This text may appear in the outline REFERENCE section (tail),
                # but NOT as the primary chapter being generated, i.e. the
                # "=== THIS CHAPTER ===" block must name chapter n, not other_n
                this_chapter_section = user_prompt.split("=== THIS CHAPTER ===")[1].split("===")[0]
                case.assertNotIn(
                    f"Chapter {other_n}",
                    this_chapter_section,
                    f"Chapter {n} prompt's THIS CHAPTER section references Chapter {other_n}"
                )
    print("OK: Temporary history is cleared between chapters (each inference is independent).")


def _test_saves_exactly_n_new_entries() -> None:
    """project.outline must contain exactly the original 8 + 4 new chapters."""
    import unittest
    from unittest.mock import patch

    worker, existing_outline = _tb_make_worker(8, 4)
    case = unittest.TestCase()

    responses = [_tb_make_plan(9, 4)] + [_tb_chapter_block(n) for n in range(9, 13)]

    with patch.object(worker, "_run_lean_inference", side_effect=responses), \
         patch("engine.workflow.storage.save_project") as mock_save, \
         patch.object(worker, "_extract_and_merge_characters"), \
         patch.object(worker, "_update_world_incremental"):

        errors = []
        results = []
        worker.error_occurred.connect(lambda msg: errors.append(msg))
        worker.step_finished.connect(lambda t, c: results.append((t, c)))
        worker._run_extend_outline()

    case.assertEqual(errors, [], f"unexpected error: {errors}")
    case.assertTrue(results, "step_finished was not emitted")

    final_numbers = WorkflowWorker._chapter_numbers_in_text(worker.project.outline)
    case.assertEqual(final_numbers, list(range(1, 13)),
                     f"expected chapters 1-12, got {final_numbers}")

    new_only = WorkflowWorker._chapter_numbers_in_text(
        worker.project.outline[len(existing_outline):]
    )
    case.assertEqual(new_only, [9, 10, 11, 12],
                     f"expected exactly 4 new chapters, got {new_only}")
    mock_save.assert_called_once()
    print("OK: Exactly 4 new entries are saved; existing chapters are untouched.")


def _test_existing_chapters_not_modified() -> None:
    """The original outline text must be preserved byte-for-byte."""
    import unittest
    from unittest.mock import patch

    worker, existing_outline = _tb_make_worker(8, 4)
    case = unittest.TestCase()

    responses = [_tb_make_plan(9, 4)] + [_tb_chapter_block(n) for n in range(9, 13)]

    with patch.object(worker, "_run_lean_inference", side_effect=responses), \
         patch("engine.workflow.storage.save_project"), \
         patch.object(worker, "_extract_and_merge_characters"), \
         patch.object(worker, "_update_world_incremental"):

        errors = []
        worker.error_occurred.connect(lambda msg: errors.append(msg))
        worker._run_extend_outline()

    case.assertEqual(errors, [], f"unexpected error: {errors}")
    case.assertTrue(
        worker.project.outline.startswith(existing_outline),
        "Existing outline chapters were modified — they must be byte-for-byte untouched."
    )
    print("OK: Existing chapters are not modified.")


def _test_fifth_chapter_never_generated() -> None:
    """When user requests 4, the model must never be called for a 5th chapter."""
    import unittest
    from unittest.mock import patch

    worker, _ = _tb_make_worker(8, 4)
    case = unittest.TestCase()

    all_responses = [_tb_make_plan(9, 4)] + [_tb_chapter_block(n) for n in range(9, 13)]
    captured: list[str] = []

    def _capture(task, system, user, max_tokens=500):
        captured.append(user)
        return all_responses.pop(0)

    with patch.object(worker, "_run_lean_inference", side_effect=_capture), \
         patch("engine.workflow.storage.save_project"), \
         patch.object(worker, "_extract_and_merge_characters"), \
         patch.object(worker, "_update_world_incremental"):

        errors = []
        worker.error_occurred.connect(lambda msg: errors.append(msg))
        worker._run_extend_outline()

    case.assertEqual(errors, [], f"unexpected error: {errors}")
    case.assertEqual(len(captured), 5,
        f"expected exactly 5 calls (1 plan + 4 chapters), got {len(captured)}")
    for prompt in captured:
        case.assertNotIn("Chapter 13", prompt,
                         "A prompt mentioned Chapter 13, which was never requested.")
    print("OK: The fifth chapter (Chapter 13) is never generated.")


if __name__ == "__main__":
    print("Running two-phase Extend Outline tests...\n")
    _test_phase1_produces_correct_number_of_plans()
    _test_phase2_makes_separate_inferences()
    _test_each_inference_receives_only_its_chapter_plan()
    _test_temporary_history_cleared_between_chapters()
    _test_saves_exactly_n_new_entries()
    _test_existing_chapters_not_modified()
    _test_fifth_chapter_never_generated()
    print("\nAll tests passed.")