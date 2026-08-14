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
from engine.patch_engine import (
    apply_patches,
    ensure_sections,
    find_relevant_section,
    format_errors_for_retry,
    merge_markdown_document,
    parse_patches,
)

logger = logging.getLogger("workflow")


class _StepTimer:
    """
    Context manager that logs how long a labeled block took. Used
    throughout the workflow (model load, each inference call, each
    task) so a slow run shows a clear per-step breakdown in the log
    instead of one opaque pause — e.g.:

        [timing] model_load(update_memory): 118.42s
        [timing] generate(update_memory chunk 1/1): 6.10s
        [timing] extract_and_merge_characters (total): 124.77s
    """
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
# Outline generation can need more passes than a single chapter: a 40-chapter
# request won't fit in one 3000-token pass, so allow more continuation rounds
# than MAX_CONTINUATIONS (which is sized for one chapter's prose).
MAX_OUTLINE_CONTINUATIONS = 8

# Default section skeleton for World & Setting. ensure_sections()/
# merge_markdown_document() use these so the document has stable,
# addressable headings for the PatchEngine to target — created lazily
# and non-destructively the first time any World update runs.
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

    def _log_prompt_if_enabled(self, label: str, messages: list[dict]) -> None:
        """
        When Settings > "Show full prompt sent to the model in the
        console/log" is on, log the exact system+user content about to be
        sent — otherwise a no-op. Gated so normal runs stay readable; only
        the token-count line (already logged elsewhere) shows by default.
        """
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
        self._log_prompt_if_enabled(task.value, messages)
        try:
            with _StepTimer(f"generate({task.value})"):
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
            # Process text in large chunks so a normal chapter/outline fits in a
            # single extraction call instead of triggering 2-3 chained ones —
            # only very long text (12+ chapter outlines) needs more than one.
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
                    '  "description": a single, vivid sentence written specifically for image generation, using this exact visual-only structure: '
                    '"Age: X. [ethnicity or race]. [height/build]. [hair color/length/style]. [eye color]. [skin tone]. [facial features]. [notable marks/scars]. [clothing/style]." '
                    'It must include the character\'s age, ethnicity or race, body build, hair, eyes, skin tone, facial structure, distinct marks, and visible clothing/style. '
                    'Do not include personality, backstory, memory, motivations, secrets, or inner emotions.\n'
                    '  "backstory": a short, factual summary of the character\'s history, upbringing, major past events, and personal context.\n'
                    '  "traits": an array of 3-6 short trait labels such as ["loyal", "guarded", "sharp-witted"].\n\n'
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

                # Budget guard: this bypasses build_context_for_model, so
                # nothing else caps prompt+reply against n_ctx here — a big
                # chunk plus a generous max_tokens could overflow context on
                # a small-n_ctx model the same way review_chapter did.
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
            existing_lower.add(name.lower())
            added += 1
        return added

    def _update_world_incremental(self, source_text: str, source_type: str = "chapter") -> None:
        """
        After the model writes an outline or chapter, quietly ask it to pull
        out ONLY hard, stable facts about the WORLD ITSELF — not plot events,
        not character feelings, not what happened in the scene — and apply
        them to World & Setting as PatchEngine ADD/REPLACE operations
        against the one relevant section, instead of resending (and then
        re-appending onto) the entire World document.

        source_type: "outline" or "chapter" — kept for call-site clarity;
        the extraction prompt itself is source-agnostic.
        Do NOT call this on synopsis text (synopsis is plot summary, not
        worldbuilding).

        World & Setting should only contain:
          ✓ Named places and their physical/geographical properties
          ✓ How magic / technology / power systems work (rules, limits, costs)
          ✓ Historical events that happened BEFORE the story begins
          ✓ Cultural norms, factions, social structures, economies
          ✓ Climate, terrain, cosmology, languages

        It must NEVER contain:
          ✗ Events that happen during the story (those belong in Story Memory)
          ✗ Character thoughts, feelings, motivations, secrets
          ✗ Dialogue or scene descriptions
          ✗ Plot outcomes or discoveries made by characters
          ✗ Relationship changes between characters
        """
        if not source_text or not source_text.strip():
            return
        _t_start = time.monotonic()
        if not self._load_model_for_task(TaskType.GENERATE_WORLD):
            logger.warning("Skipping world update: no model assigned for World generation.")
            return

        self.step_started.emit("Checking for new world details...")
        try:
            language = self._response_language()
            language_note = f" Write it in {language}." if language else ""

            # Non-destructive: only adds missing section headings, never touches
            # existing content. Gives the PatchEngine stable targets to ADD into.
            self.project.world = ensure_sections(self.project.world, DEFAULT_WORLD_SECTIONS)

            system_content = (
                "You are a strict worldbuilding archivist maintaining a Markdown "
                "reference document (World & Setting) for a novel. You work "
                "EXCLUSIVELY through SEARCH/REPLACE-style patch blocks — you "
                "never rewrite or re-output the document.\n\n"
                "Extract ONLY permanent, stable facts about the WORLD ITSELF — "
                "facts that would still be true even if the story's characters "
                "had never been born:\n"
                "- Named locations with physical/geographical properties\n"
                "- How magic, technology, or power systems work — rules, limits, costs\n"
                "- Historical events that occurred BEFORE the story begins\n"
                "- Cultural norms, factions, economies, social hierarchies\n"
                "- Climate, terrain, cosmology, calendar, languages\n\n"
                "NEVER include: plot events/discoveries/battles/decisions (those "
                "belong in Story Memory, not here), character feelings/thoughts/"
                "secrets/dialogue, scene descriptions, relationship changes, or "
                "anything already present in the section shown below.\n\n"
                "If there is nothing new and permanent to record, respond with "
                "EXACTLY: NO_NEW_WORLD_DETAILS\n\n"
                "Otherwise respond with ONLY one or more patch blocks — no other "
                "commentary, no restated document:\n\n"
                "<<<<<<< ADD\n"
                "SECTION: <one of the existing section names shown below, or a "
                "new one if truly none fit>\n"
                "=======\n"
                "- new fact as a single concise bullet\n"
                ">>>>>>> ADD\n\n"
                "Use REPLACE only to correct/refine a bullet that's already "
                "there (SEARCH must match it exactly, verbatim):\n\n"
                "<<<<<<< REPLACE\n"
                "old exact bullet\n"
                "=======\n"
                "new bullet\n"
                ">>>>>>> REPLACE"
                f"{language_note}"
            )

            # Same reasoning as _extract_and_merge_characters: a bigger chunk
            # means a normal chapter/outline needs only one patch call instead
            # of 2-3 chained ones.
            CHUNK_SIZE = 16000
            text_chunks = [
                source_text[i:i + CHUNK_SIZE]
                for i in range(0, max(1, len(source_text)), CHUNK_SIZE)
            ]
            for chunk_idx, chunk in enumerate(text_chunks):
                if self._cancelled:
                    break

                with _StepTimer(f"world_patch chunk {chunk_idx + 1}/{len(text_chunks)} (total, incl. retry)"):
                    relevant_section = find_relevant_section(self.project.world, chunk)
                    section_context = relevant_section or (
                        "(no closely related section found — ADD a new SECTION for this)"
                    )
                    all_sections = ", ".join(DEFAULT_WORLD_SECTIONS)
                    user_content = (
                        f"Existing section names: {all_sections} (plus any custom ones "
                        f"already in the document).\n\n"
                        f"Current relevant section of World & Setting:\n{section_context}\n\n"
                        f"New text to scan for permanent world facts:\n{chunk}"
                    )

                    raw = self._run_lean_inference(
                        TaskType.GENERATE_WORLD, system_content, user_content, max_tokens=350,
                    )
                    if not raw:
                        continue
                    cleaned = raw.strip()
                    if not cleaned or "NO_NEW_WORLD_DETAILS" in cleaned.upper():
                        continue

                    patches, parse_warnings = parse_patches(cleaned)
                    if not patches:
                        logger.info(
                            f"[world_patch] chunk {chunk_idx + 1}: no valid patch blocks "
                            f"({parse_warnings}) — skipping."
                        )
                        continue

                    result = apply_patches(self.project.world, patches)
                    if not result.success:
                        # One retry with the validation errors fed back — never
                        # silently apply to the wrong text, never fall back to a
                        # full rewrite.
                        retry_user = user_content + "\n\n" + format_errors_for_retry(result.errors)
                        raw_retry = self._run_lean_inference(
                            TaskType.GENERATE_WORLD, system_content, retry_user, max_tokens=350,
                        )
                        if raw_retry:
                            retry_patches, _ = parse_patches(raw_retry.strip())
                            if retry_patches:
                                result = apply_patches(self.project.world, retry_patches)

                    if result.success:
                        self.project.world = result.document
                        logger.info(
                            f"[world_patch] chunk {chunk_idx + 1}/{len(text_chunks)}: "
                            f"applied {result.applied} patch(es)."
                        )
                    else:
                        logger.warning(
                            f"[world_patch] chunk {chunk_idx + 1}: failed to apply after "
                            f"retry — left unchanged ({[e.reason for e in result.errors]})."
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
        Minimal-context inference call used by the PatchEngine round-trip
        (World incremental updates, Chapter patch generation).

        Unlike `_run_inference_v2`, this does NOT pull in Synopsis,
        Outline, full Characters, Memory, or chat history via
        `build_context_for_model` — it sends only the exact
        system/user content the caller built (typically: the task
        instructions + the one relevant excerpt of the document being
        edited). That's the actual token reduction this feature is
        for: the full document stays on disk, only the fragment needed
        for this one edit goes to the model.
        """
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
        logger.info(
            f"[{task.value}] lean inference: prompt_tokens={prompt_tokens}, "
            f"max_tokens={effective_max_tokens}, temperature={temperature}"
        )

        engine = get_engine()
        self._log_prompt_if_enabled(f"lean:{task.value}", messages)
        try:
            with _StepTimer(f"lean_generate({task.value})"):
                result = engine.generate(
                    messages=messages,
                    max_tokens=effective_max_tokens,
                    temperature=temperature,
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
            self._update_world_incremental(result, source_type="chapter")
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
            # Deliberately NOT calling _update_world_incremental here:
            # a synopsis is a plot summary, not a worldbuilding document.
            # Running world extraction on it produces false positives (plot
            # events, character feelings) that pollute World & Setting.
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
            self._update_world_incremental(outline_text, source_type="outline")

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
            TaskType.GENERATE_WORLD, prompt, add_to_chat=True, max_tokens=800
        )
        if result:
            # Fold the new text into the existing document by section
            # instead of concatenating a growing "---"-separated blob —
            # matching headings are merged, new ones become new sections.
            self.project.world = merge_markdown_document(
                self.project.world, result, DEFAULT_WORLD_SECTIONS
            )
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
        Ask the model whether the chapter draft is complete.

        Logs every attempt verbosely so the Console panel can show the
        full evaluation flow in real time. Never falls back silently —
        if all retries return an unparseable response the final result is
        still logged as INVALID so the user can see what the model said.
        """
        word_count = len(chapter_text.split())
        logger.info(
            "[eval] ── Chapter %d completion check (after pass %d) ──  "
            "%d words written so far",
            chapter_num, generation_pass, word_count,
        )

        system_content = (
            "You decide whether a novel chapter draft is complete relative to "
            "its stated goal. Respond with exactly one token: true or false. "
            "No punctuation, quotes, markdown, JSON, or explanation.\n"
            "true  → the chapter is complete and should stop.\n"
            "false → the chapter is not complete and must continue."
        )
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
            # All retries exhausted without a clean answer.
            # Treat as "not complete" so the loop tries one more continuation
            # rather than truncating silently — but log it loudly so the user
            # can see what happened.
            logger.warning(
                "[eval] All %d attempts failed to get a clean true/false. "
                "Last raw output: %r  — treating as FALSE (will continue).",
                MAX_COMPLETION_EVAL_RETRIES, final.get("raw", ""),
            )
            final["completed"] = False

        action = "STOP — chapter complete." if final["completed"] else "CONTINUE — more text needed."
        logger.info("[eval] ── Result for Chapter %d: %s ──", chapter_num, action)
        return final

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
                generation_pass=generation_pass,
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
            self._update_world_incremental(chapter_text, source_type="chapter")
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
            with _StepTimer(f"write_book chapter {pending}/{total} (total)"):
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
        """
        Turns Review feedback into a chapter fix WITHOUT asking the model
        to re-output the chapter. The model receives the chapter text (it
        needs it to write exact SEARCH text) but is instructed to respond
        with ONLY PatchEngine SEARCH/REPLACE/DELETE blocks; those are
        validated and applied to `chapter.content` in place. If validation
        or application fails (even after one retry with the error fed
        back), `chapter.content` is left completely untouched — there is
        no silent partial edit and no fallback to a full rewrite.
        """
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

        self.step_started.emit(f"Patching Chapter {chapter_num} from review feedback...")

        style_frag = self.project.writing_style.to_prompt_fragment()
        intent = self.project.author_intent
        intent_lines = []
        if intent.emotional_journey:
            intent_lines.append(f"Emotional tone to sustain: {intent.emotional_journey}")
        if intent.avoid:
            intent_lines.append(f"Avoid entirely: {intent.avoid}")

        language = self._response_language()
        language_note = f" Write any new/changed text in {language}." if language else ""

        system_content = (
            "You are a precise novel line-editor. You fix a chapter by "
            "emitting SEARCH/REPLACE (or DELETE) patch blocks against its "
            "EXACT existing text. You NEVER rewrite or re-output the "
            "chapter, even for a single-word fix — only the blocks below.\n\n"
            "Respond with ONLY patch blocks, one per issue, no other text:\n\n"
            "<<<<<<< REPLACE\n"
            "exact text to change, copied verbatim from the chapter "
            "(whitespace and punctuation must match exactly)\n"
            "=======\n"
            "corrected text\n"
            ">>>>>>> REPLACE\n\n"
            "For a pure deletion:\n"
            "<<<<<<< DELETE\n"
            "exact text to remove\n"
            ">>>>>>> DELETE\n\n"
            "SEARCH must match the chapter text exactly and uniquely — keep "
            "each SEARCH block as short as possible while staying "
            "unambiguous. Do not include untouched surrounding paragraphs."
            f"{language_note}"
            + (f"\n\nStyle to preserve:\n{style_frag}" if style_frag else "")
            + (("\n\n" + "\n".join(intent_lines)) if intent_lines else "")
        )

        user_content = (
            f"Chapter {chapter_num}: '{chapter.title}'\n\n"
            f"Current content:\n{chapter.content}\n\n"
            f"Review feedback to address:\n{chapter.last_review}"
        )
        if self.extra_input:
            user_content += f"\n\nAdditional author note: {self.extra_input}"

        raw = self._run_lean_inference(
            TaskType.REWRITE_CHAPTER, system_content, user_content,
            max_tokens=self._content_max_tokens(),
        )
        if not raw:
            self.error_occurred.emit("The model returned no patch for this chapter.")
            return

        patches, parse_warnings = parse_patches(raw)
        if not patches:
            logger.warning(f"[chapter_patch] no valid patch blocks ({parse_warnings}).")
            self.error_occurred.emit(
                "The model's response didn't contain any valid patch blocks. "
                "Try \"Rewrite with Feedback\" again."
            )
            return

        original_content = chapter.content
        result = apply_patches(original_content, patches)

        if not result.success:
            retry_user = user_content + "\n\n" + format_errors_for_retry(result.errors)
            raw_retry = self._run_lean_inference(
                TaskType.REWRITE_CHAPTER, system_content, retry_user,
                max_tokens=self._content_max_tokens(),
            )
            if raw_retry:
                retry_patches, _ = parse_patches(raw_retry)
                if retry_patches:
                    result = apply_patches(original_content, retry_patches)

        if not result.success:
            reasons = "; ".join(e.reason for e in result.errors) or "unknown error"
            logger.warning(f"[chapter_patch] Chapter {chapter_num}: patch failed after retry ({reasons}).")
            self.error_occurred.emit(
                f"Could not apply the model's patch to Chapter {chapter_num} even after "
                f"a retry ({reasons}). The chapter was left unchanged — try Review again."
            )
            return

        chapter.content = result.document
        chapter.reviewed = False  # it changed — worth another look before moving on
        chapter.last_review = ""
        self._extract_and_merge_characters(result.document)

        # _run_lean_inference deliberately doesn't touch chat (raw
        # SEARCH/REPLACE blocks aren't something the author needs to see) —
        # add a short human-readable summary instead, same pattern used by
        # chapter continuation.
        self.project.chat_messages.append(ChatMessage(
            role=MessageRole.USER,
            content=self.extra_input or f"Rewrite Chapter {chapter_num} with review feedback",
        ))
        self.project.chat_messages.append(ChatMessage(
            role=MessageRole.ASSISTANT,
            content=f"*(Applied {result.applied} patch(es) to Chapter {chapter_num} based on the review feedback.)*",
        ))

        storage.save_project(self.project)
        self.step_finished.emit(
            f"Patched Chapter {chapter_num} ({result.applied} change(s) applied)",
            result.document,
        )

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
        chapter_content = chapter.content

        # This call goes through _run_lean_inference (system+user only, no
        # build_context_for_model) specifically because World/Characters/
        # Memory are already embedded below by hand — running it through the
        # normal context builder would inject those same three sections a
        # second time into the system prompt, wasting tokens and, on a full
        # chapter, risking a context overflow (this used to happen here).
        #
        # Cap each embedded section defensively too, keeping the END of the
        # chapter — Story Memory only cares about where things stand as of
        # the chapter's ending, not its opening, which is what's most likely
        # to already be covered by World/Characters/the previous Memory.
        WORLD_CAP, CHAR_CAP, MEMORY_CAP, CHAPTER_CAP = 2000, 1500, 2500, 14000
        if len(world) > WORLD_CAP:
            world = "[...]\n" + world[-WORLD_CAP:]
        if len(characters) > CHAR_CAP:
            characters = "[...]\n" + characters[-CHAR_CAP:]
        if len(existing_memory) > MEMORY_CAP:
            existing_memory = "[...]\n" + existing_memory[-MEMORY_CAP:]
        if len(chapter_content) > CHAPTER_CAP:
            chapter_content = "[... earlier part of the chapter omitted for length ...]\n\n" + chapter_content[-CHAPTER_CAP:]

        system_content = (
            "You are a story-continuity editor updating the STORY MEMORY for a novel.\n\n"
            "Story Memory is a WORKING NOTES document for the WRITER. Its only purpose "
            "is to help write the NEXT chapter without contradicting what already happened.\n\n"
            "The following are stored SEPARATELY — do NOT copy anything from the chapter "
            "into these categories:\n"
            "- World & Setting already tracks: geography, locations, how magic/tech "
            "works, history, culture, factions\n"
            "- Characters already tracks: names, roles, physical appearance, personality\n\n"
            "Rewrite the Story Memory sections below. Keep ONLY what a writer needs to "
            "avoid contradictions in the NEXT chapter.\n\n"
            "INCLUDE:\n"
            "- Where characters physically are RIGHT NOW at the end of this chapter\n"
            "- What is actively happening or about to happen (current crisis, mission, scene)\n"
            "- What each main character is trying to do and why (only if it changed this chapter)\n"
            "- Relationship shifts that happened in THIS chapter (conflict, alliance, betrayal)\n"
            "- Objects, items, or states that changed hands or status\n"
            "- Plot threads that were OPENED but not resolved in this chapter\n\n"
            "EXCLUDE (these belong in World & Setting or Characters, not here):\n"
            "- Descriptions of places (geography, architecture) — already in World & Setting\n"
            "- How magic or technology works — already in World & Setting\n"
            "- Physical appearance of characters — already in Characters\n"
            "- Backstory or history — already in World & Setting\n"
            "- Anything that was already resolved and no longer affects the next chapter\n\n"
            "OUTPUT FORMAT — use exactly these section headers, leave a section empty "
            "rather than filling it with World/Character data:\n\n"
            "# Current Location\n"
            "(where the main characters are right now, one line each)\n\n"
            "# Current Situation\n"
            "(what is actively happening at the end of this chapter)\n\n"
            "# Active Goals\n"
            "(what each main character is trying to achieve RIGHT NOW)\n\n"
            "# Relationship Changes This Chapter\n"
            "(only changes that happened in this chapter)\n\n"
            "# Item / Status Changes\n"
            "(objects, abilities, or conditions that changed)\n\n"
            "# Open Plot Threads\n"
            "(unresolved threads from this and previous chapters)\n\n"
            "Output ONLY the updated Story Memory. No explanation. No markdown code fences."
        )

        user_content = (
            "==================================================\n"
            "WORLD & SETTING (already tracked — do not repeat)\n"
            "==================================================\n"
            f"{world}\n\n"
            "==================================================\n"
            "CHARACTERS (already tracked — do not repeat)\n"
            "==================================================\n"
            f"{characters}\n\n"
            "==================================================\n"
            "CURRENT STORY MEMORY (update this)\n"
            "==================================================\n"
            f"{existing_memory}\n\n"
            "==================================================\n"
            "NEW CHAPTER TO PROCESS\n"
            "==================================================\n"
            f"Chapter {chapter_num}: {chapter.title}\n\n"
            f"{chapter_content}"
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