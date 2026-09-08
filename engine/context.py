"""
Context builder for model inference.

The task-specific system prompt is authoritative and is kept intact. Story
context is added around it and compacted only when the model context window
requires it.
"""

from __future__ import annotations

import logging
from typing import Optional

from engine import prompts
from engine.models import ChatMessage, MessageRole, Project, TaskType

logger = logging.getLogger("context")


CUSTOM_INSTRUCTIONS_MARKER = "## Additional Author Instructions"


def _format_character_for_prompt(c) -> str:
    lines = [f"### {c.name}", f"Role: {c.role}", f"Description: {c.description}"]
    backstory = getattr(c, "backstory", "")
    if backstory:
        lines.append(f"Backstory: {backstory}")
    traits = getattr(c, "traits", [])
    if traits:
        lines.append("Traits:")
        lines.extend(f"- {t}" for t in traits)
    relationships = getattr(c, "relationships", [])
    if relationships:
        lines.append("Relationships:")
        lines.extend(f"- {r.to_prompt_line()}" for r in relationships)
    return "\n".join(lines)


def format_characters_block(characters: list) -> str:
    return "\n\n".join(_format_character_for_prompt(c) for c in characters).strip()


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _messages_token_count(messages: list[dict]) -> int:
    return sum(_estimate_tokens(m.get("content", "")) for m in messages)


def _section_tokens(text: str) -> int:
    return _estimate_tokens(text) if text and text.strip() else 0


def _truncate_to_budget(text: str, budget: int) -> str:
    text = (text or "").strip()
    if not text or budget <= 0:
        return ""
    approx_chars = max(64, budget * 4)
    if len(text) <= approx_chars:
        return text
    return text[: max(0, approx_chars - 48)].rstrip() + "\n\n[... truncated to fit context budget ...]"


def extract_outline_section(outline: str, chapter_number: int) -> str:
    outline = (outline or "").strip()
    if not outline:
        return ""
    lines = outline.splitlines()
    capture = False
    result: list[str] = []
    target = f"## Chapter {chapter_number}"
    next_marker = f"## Chapter {chapter_number + 1}"
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


def _estimate_task_instruction(task: TaskType) -> str:
    try:
        return prompts.load_raw(f"task_instructions/{task.value}")
    except FileNotFoundError:
        return ""


def _section_payloads(
    project: Project,
    task: TaskType,
    system_prompt: str,
    user_message: str = "",
) -> dict[str, str]:
    synopsis = f"Story Synopsis:\n{project.synopsis.strip()}" if project.synopsis.strip() else ""
    characters = format_characters_block(project.characters)
    world = project.world.strip()
    memory = project.memory.strip()
    chat_summary = project.chat_summary.strip()

    creative_direction = ""
    intent = project.author_intent
    style = project.writing_style
    if task == TaskType.GENERATE_OUTLINE:
        parts = [p for p in (intent.to_prompt_fragment(), style.to_prompt_fragment()) if p]
        creative_direction = "\n".join(parts)
    elif task in (TaskType.WRITE_CHAPTER, TaskType.REWRITE_CHAPTER, TaskType.CHANGE_CHAPTER):
        parts = []
        style_frag = style.to_prompt_fragment()
        if style_frag:
            parts.append(style_frag)
        intent_lines = []
        if intent.emotional_journey:
            intent_lines.append(f"Reader's emotional experience to sustain: {intent.emotional_journey}")
        if intent.avoid:
            intent_lines.append(f"Avoid entirely: {intent.avoid}")
        if intent_lines:
            parts.append("\n".join(intent_lines))
        creative_direction = "\n".join(parts)
    elif task == TaskType.REVIEW_CHAPTER:
        parts = [p for p in (intent.to_prompt_fragment(), style.to_prompt_fragment()) if p]
        creative_direction = "\n".join(parts)

    outline = ""
    if task != TaskType.GENERATE_OUTLINE and project.outline.strip():
        if task in (TaskType.WRITE_CHAPTER, TaskType.REWRITE_CHAPTER, TaskType.CHANGE_CHAPTER):
            chapter_num = project.current_chapter or max((c.number for c in project.chapters), default=1)
            specific = extract_outline_section(project.outline, chapter_num)
            # First-pass writing already embeds the complete chapter outline in
            # the user message. Continuations do not, so keep the specific
            # outline in system context for those passes.
            if not (
                task == TaskType.WRITE_CHAPTER
                and "CHAPTER OUTLINE (binding)" in (user_message or "")
            ):
                outline = specific or project.outline.strip()
        else:
            outline = project.outline.strip()

    # Generate Outline already receives the requested story material in the
    # user message. Avoid duplicating it in system context when present.
    if task == TaskType.GENERATE_OUTLINE and "synopsis:" in (user_message or "").lower():
        synopsis = ""

    return {
        "Synopsis": synopsis,
        "Characters": characters,
        "Outline": outline,
        "World": world,
        "Memory": memory,
        "Chat Summary": chat_summary,
        "Creative Direction": creative_direction,
    }


def _compact_sections(
    sections: dict[str, str],
    max_context_tokens: int,
    task: TaskType,
    project: Project,
) -> dict[str, str]:
    # These are caps for story context only. The full task/system prompt is
    # kept intact and is not subjected to these budgets.
    if task == TaskType.GENERATE_OUTLINE:
        budgets = {
            "Synopsis": 0,
            "Characters": max(220, min(700, max_context_tokens // 8)),
            "Outline": 0,
            "World": max(180, min(500, max_context_tokens // 10)),
            "Memory": 0,
            "Chat Summary": 0,
            "Creative Direction": max(120, min(360, max_context_tokens // 12)),
        }
    elif task in (TaskType.WRITE_CHAPTER, TaskType.REWRITE_CHAPTER, TaskType.CHANGE_CHAPTER):
        budgets = {
            "Synopsis": max(180, min(450, max_context_tokens // 10)),
            "Characters": max(240, min(650, max_context_tokens // 7)),
            "Outline": max(450, min(1100, max_context_tokens // 4)),
            "World": max(80, min(350, max_context_tokens // 12)),
            "Memory": max(100, min(450, max_context_tokens // 10)),
            "Chat Summary": 0,
            "Creative Direction": max(80, min(300, max_context_tokens // 14)),
        }
    else:
        budgets = {
            "Synopsis": max(120, max_context_tokens // 20),
            "Characters": max(180, max_context_tokens // 14),
            "Outline": max(350, max_context_tokens // 8),
            "World": max(80, max_context_tokens // 32),
            "Memory": max(150, max_context_tokens // 16),
            "Chat Summary": max(100, max_context_tokens // 24),
            "Creative Direction": max(60, max_context_tokens // 20),
        }

    if len(project.characters) > 12:
        sections["Characters"] = format_characters_block(project.characters[:12])

    for name, text in list(sections.items()):
        sections[name] = _truncate_to_budget(text, budgets.get(name, 0))
    return sections


def _split_custom_instructions(system_prompt: str) -> tuple[str, str]:
    if CUSTOM_INSTRUCTIONS_MARKER not in system_prompt:
        return system_prompt.strip(), ""
    core, custom = system_prompt.split(CUSTOM_INSTRUCTIONS_MARKER, 1)
    return core.rstrip(), custom.strip()


def _build_story_context_text(
    project: Project,
    task: TaskType,
    system_prompt: str,
    user_message: str,
    max_context_tokens: int,
    include_story_context: bool,
) -> str:
    core_system, custom_instructions = _split_custom_instructions(system_prompt)
    parts = [core_system]
    if include_story_context:
        sections = _compact_sections(
            _section_payloads(project, task, system_prompt, user_message),
            max_context_tokens=max_context_tokens,
            task=task,
            project=project,
        )
        if task == TaskType.GENERATE_OUTLINE:
            # Synopsis is normally in the user message for outline generation.
            # Characters and World are reference canon for planning; keep them
            # compact so the detailed task prompt remains available.
            if sections["Characters"]:
                parts.append(f"\n\n## Established Characters\n{sections['Characters']}")
            if sections["World"]:
                parts.append(f"\n\n## World & Setting\n{sections['World']}")
            if sections["Creative Direction"]:
                parts.append(f"\n\n## Author's Creative Direction\n{sections['Creative Direction']}")
        else:
            if sections["Synopsis"]:
                parts.append(f"\n\n{sections['Synopsis']}")
            if sections["Characters"]:
                parts.append(f"\n\n## Established Characters\n{sections['Characters']}")
            if sections["Outline"]:
                parts.append(f"\n\n## Outline\n{sections['Outline']}")
            if sections["World"]:
                parts.append(f"\n\n## World & Setting\n{sections['World']}")
            if sections["Memory"]:
                parts.append(f"\n\n## Story Memory\n{sections['Memory']}")
            if sections["Chat Summary"]:
                parts.append(f"\n\n## Conversation Summary (older messages)\n{sections['Chat Summary']}")
            if sections["Creative Direction"]:
                parts.append(f"\n\n## Author's Creative Direction\n{sections['Creative Direction']}")

    # Author-provided custom instructions remain the final instruction block.
    if custom_instructions:
        parts.append(f"\n\n{CUSTOM_INSTRUCTIONS_MARKER}\n{custom_instructions}")
    return "\n".join(p for p in parts if p).strip()


def _repair_alternation(messages: list[dict]) -> list[dict]:
    while messages and messages[0].get("role") != "user":
        messages.pop(0)
    repaired: list[dict] = []
    for message in messages:
        if repaired and repaired[-1]["role"] == message["role"]:
            repaired[-1]["content"] += "\n\n" + message["content"]
        else:
            repaired.append(dict(message))
    return repaired


def build_context_for_model(
    project: Project,
    user_message: str,
    system_prompt: str,
    max_context_tokens: int = 3200,
    task: TaskType = TaskType.CHAT,
    reply_reserved: Optional[int] = None,
    include_story_context: bool = True,
) -> list[dict]:
    """Build [system, recent chat, user] while preserving the complete task prompt."""
    system_content = _build_story_context_text(
        project,
        task,
        system_prompt,
        user_message,
        max_context_tokens,
        include_story_context,
    )

    # Outline generation and chapter writing are deterministic workflow steps;
    # chat history only consumes context and can conflict with the task prompt.
    use_history = task not in {
        TaskType.GENERATE_OUTLINE,
        TaskType.WRITE_CHAPTER,
        TaskType.REWRITE_CHAPTER,
        TaskType.CHANGE_CHAPTER,
    }
    eligible = [
        m for m in project.chat_messages
        if use_history
        and m.role in (MessageRole.USER, MessageRole.ASSISTANT)
        and not m.summarized
    ]
    recent = [
        {"role": m.role.value, "content": m.content}
        for m in eligible[-project.recent_message_window:]
    ]

    reply_headroom = reply_reserved if reply_reserved is not None else max(256, min(4096, max_context_tokens // 3))
    system_tokens = _estimate_tokens(system_content)
    user_tokens = _estimate_tokens(user_message)
    history_tokens = _messages_token_count(recent)
    total = system_tokens + user_tokens + history_tokens + reply_headroom
    while total > max_context_tokens and recent:
        removed = recent.pop(0)
        total -= _estimate_tokens(removed["content"])

    recent = _repair_alternation(recent)
    if recent and recent[-1]["role"] == "user":
        recent[-1]["content"] += "\n\n" + user_message
        trailing_user = None
    else:
        trailing_user = {"role": "user", "content": user_message}

    messages: list[dict] = [{"role": "system", "content": system_content}]
    messages.extend(recent)
    if trailing_user is not None:
        messages.append(trailing_user)

    reserved = reply_reserved if reply_reserved is not None else max(256, min(4096, max_context_tokens // 3))
    non_user_tokens = _messages_token_count(messages[:-1]) if trailing_user is not None else _messages_token_count(messages)
    target = messages[-1] if messages and messages[-1].get("role") == "user" else None
    if target is not None:
        budget_for_user = max(200, max_context_tokens - non_user_tokens - reserved - 64)
        if _estimate_tokens(target["content"]) > budget_for_user:
            approx_chars = max(200, budget_for_user * 4)
            content = target["content"]
            target["content"] = (
                "[... earlier content truncated to fit context budget ...]\n\n"
                + content[-approx_chars:].lstrip()
            )
    return messages


def build_review_context_for_model(
    project: Project,
    user_message: str,
    system_prompt: str,
    max_context_tokens: int = 3200,
    reply_reserved: Optional[int] = None,
) -> list[dict]:
    """Build a lean review context without chat history."""
    core_system, custom_instructions = _split_custom_instructions(system_prompt)
    chapter_num = project.current_chapter or max((c.number for c in project.chapters), default=1)
    outline = extract_outline_section(project.outline, chapter_num)

    parts = [core_system]
    if project.synopsis.strip():
        synopsis = _truncate_to_budget(f"Story Synopsis:\n{project.synopsis.strip()}", max(180, max_context_tokens // 12))
        parts.append(synopsis)
    characters = _truncate_to_budget(format_characters_block(project.characters), max(180, max_context_tokens // 10))
    if characters:
        parts.append(f"## Established Characters\n{characters}")
    if outline:
        parts.append(f"## Outline\n{_truncate_to_budget(outline, max(300, max_context_tokens // 6))}")
    if custom_instructions:
        parts.append(f"\n\n{CUSTOM_INSTRUCTIONS_MARKER}\n{custom_instructions}")

    system_content = "\n\n".join(p for p in parts if p).strip()
    reserved = reply_reserved if reply_reserved is not None else 512
    system_tokens = _estimate_tokens(system_content)
    budget_for_user = max(200, max_context_tokens - system_tokens - reserved - 64)
    if _estimate_tokens(user_message) > budget_for_user:
        approx_chars = max(200, budget_for_user * 4)
        user_message = "[... earlier content truncated to fit context budget ...]\n\n" + user_message[-approx_chars:].lstrip()
    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_message},
    ]


def estimate_context_usage(
    project: Project,
    user_message: str,
    system_prompt: str,
    max_context_tokens: int = 3200,
    task: TaskType = TaskType.CHAT,
    reply_reserved: Optional[int] = None,
    requested_max_tokens: Optional[int] = None,
) -> dict:
    messages = build_context_for_model(
        project,
        user_message,
        system_prompt,
        max_context_tokens=max_context_tokens,
        task=task,
        reply_reserved=reply_reserved,
    )
    prompt_tokens = _messages_token_count(messages)
    available_reply_tokens = max(0, max_context_tokens - prompt_tokens)
    requested_reply = requested_max_tokens if requested_max_tokens is not None else (
        reply_reserved if reply_reserved is not None else max(256, min(4096, max_context_tokens // 3))
    )
    effective_max_tokens = min(requested_reply, available_reply_tokens)
    return {
        "sections": {},
        "prompt_tokens": prompt_tokens,
        "system_tokens": _estimate_tokens(messages[0].get("content", "")) if messages else 0,
        "user_tokens": _estimate_tokens(messages[-1].get("content", "")) if messages else 0,
        "history_tokens": _messages_token_count(messages[1:-1]) if len(messages) > 2 else 0,
        "available_reply_tokens": available_reply_tokens,
        "reply_headroom": reply_reserved or 0,
        "requested_max_tokens": requested_reply,
        "effective_max_tokens": effective_max_tokens,
        "estimated_total": prompt_tokens + effective_max_tokens,
        "estimated_remaining": max(0, available_reply_tokens - effective_max_tokens),
        "max_context_tokens": max_context_tokens,
        "recent_messages": max(0, len(messages) - 2),
    }


def estimate_messages_tokens(messages: list[dict]) -> int:
    return _messages_token_count(messages)


def should_summarize(project: Project, threshold: int = 30) -> bool:
    unsummarized = [
        m for m in project.chat_messages
        if m.role in (MessageRole.USER, MessageRole.ASSISTANT) and not m.summarized
    ]
    return len(unsummarized) > threshold + project.recent_message_window


def mark_old_messages_summarized(project: Project) -> list[ChatMessage]:
    eligible = [
        m for m in project.chat_messages
        if m.role in (MessageRole.USER, MessageRole.ASSISTANT) and not m.summarized
    ]
    to_summarize = eligible[: max(0, len(eligible) - project.recent_message_window)]
    for message in to_summarize:
        message.summarized = True
    return to_summarize


def build_summarization_prompt(messages_to_summarize: list[ChatMessage]) -> str:
    history_text = "\n".join(
        f"{m.role.value.upper()}: {m.content}" for m in messages_to_summarize
    )
    return (
        "The following is a portion of a story development conversation that needs to be summarized. "
        "Write a concise but complete summary that captures:\n"
        "- Key decisions made about the story\n"
        "- Characters discussed or created\n"
        "- Plot points established\n"
        "- Writing feedback given\n"
        "- Any important context future messages might need\n\n"
        "Conversation to summarize:\n"
        "---\n"
        f"{history_text}\n"
        "---\n\n"
        "Write only the summary, no preamble."
    )


def build_system_prompt(
    project: Project,
    task: TaskType,
    custom_instructions: str = "",
    language: str = "",
    allow_nsfw: bool = False,
) -> str:
    """Build the complete task system prompt. It must not be silently truncated."""
    creative_tasks = {
        TaskType.WRITE_SYNOPSIS,
        TaskType.GENERATE_OUTLINE,
        TaskType.WRITE_CHAPTER,
        TaskType.REWRITE_CHAPTER,
        TaskType.CHANGE_CHAPTER,
        TaskType.GENERATE_WORLD,
        TaskType.UPDATE_MEMORY,
    }
    base = (
        f"You are an AI assistant helping to write a novel titled '{project.title}'. "
        "You are deeply familiar with the story world, characters, and plot. "
        "You respond only in the context of this story."
    )
    if language and language.strip():
        base += (
            f" IMPORTANT: Always write your response in {language.strip()}, "
            "regardless of the language these instructions are written in."
        )
    if allow_nsfw and task in creative_tasks:
        base += (
            "\n\nCONTENT POLICY: This is an adult fiction project. "
            "You are explicitly permitted — and expected when the story requires it — "
            "to write mature content without restrictions: graphic violence, explicit "
            "sexuality, strong language, disturbing themes, morally complex or reprehensible "
            "characters, and dark subject matter. Do not soften, fade to black, skip, or "
            "summarize scenes that the story calls for. Write them fully and directly. "
            "No content filtering is applied to this project."
        )
    instruction = _estimate_task_instruction(task)
    prompt = base.strip()
    if instruction:
        prompt += f"\n\n{instruction}"
    if custom_instructions and custom_instructions.strip():
        prompt += f"\n\n{CUSTOM_INSTRUCTIONS_MARKER}\n{custom_instructions.strip()}"
    return prompt
