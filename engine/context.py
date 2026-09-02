"""
Context builder: assembles the message list sent to the model, managing
chat history trimming and section budgets.
"""

from __future__ import annotations

import logging
from typing import Optional

from engine import prompts
from engine.models import ChatMessage, MessageRole, Project, TaskType

logger = logging.getLogger("context")


def _format_character_for_prompt(c) -> str:
    """Format a single character as a structured block for model context.
    Sections are omitted when empty. Relationships use the full "relation target" form.
    """
    lines = [f"### {c.name}", f"Role: {c.role}", f"Description: {c.description}"]
    backstory = getattr(c, "backstory", "")
    if backstory:
        lines.append(f"Backstory: {backstory}")
    traits = getattr(c, "traits", [])
    if traits:
        lines.append("Traits:")
        for t in traits:
            lines.append(f"- {t}")
    rels = getattr(c, "relationships", [])
    if rels:
        lines.append("Relationships:")
        for r in rels:
            lines.append(f"- {r.to_prompt_line()}")
    return "\n".join(lines)


def format_characters_block(characters: list) -> str:
    """Format all characters (with relationships) into a single string."""
    return "\n\n".join(_format_character_for_prompt(c) for c in characters).strip()



def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _messages_token_count(messages: list[dict]) -> int:
    total = 0
    for m in messages:
        total += _estimate_tokens(m.get("content", ""))
    return total


def _section_tokens(text: str) -> int:
    return _estimate_tokens(text) if text.strip() else 0


def _truncate_to_budget(text: str, budget: int) -> str:
    text = text.strip()
    if not text or budget <= 0:
        return ""
    approx_chars = max(64, budget * 4)
    if len(text) <= approx_chars:
        return text
    return text[: max(0, approx_chars - 48)].rstrip() + "\n\n[... truncated to fit context budget ...]"


def extract_outline_section(outline: str, chapter_number: int) -> str:
    outline = outline.strip()
    if not outline:
        return ""
    lines = outline.split("\n")
    capture = False
    result = []
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
    """Load the task instruction text from engine/prompts/task_instructions/. Returns "" if absent."""
    try:
        return prompts.load_raw(f"task_instructions/{task.value}")
    except FileNotFoundError:
        return ""


def _section_payloads(
    project: Project,
    task: TaskType,
    system_prompt: str,
) -> dict[str, str]:
    base = (
        f"You are an AI assistant helping to write a novel titled '{project.title}'. "
        "You are deeply familiar with the story world, characters, and plot. "
        "You respond only in the context of this story."
    )
    language = ""
    marker = "IMPORTANT: Always write your response in "
    if marker in system_prompt:
        tail = system_prompt.split(marker, 1)[1]
        language = tail.split(",", 1)[0].strip()
    if language:
        language = (
            f"IMPORTANT: Always write your response in {language}, "
            "regardless of the language these instructions are written in."
        )

    synopsis = f"Story Synopsis:\n{project.synopsis.strip()}" if project.synopsis.strip() else ""
    characters = format_characters_block(project.characters)
    outline = project.outline.strip() if project.outline.strip() and task != TaskType.GENERATE_OUTLINE else ""
    world = project.world.strip()
    memory = project.memory.strip()
    chat_summary = project.chat_summary.strip()
    author_instructions = ""
    if "## Additional Author Instructions" in system_prompt:
        author_instructions = system_prompt.split("## Additional Author Instructions", 1)[1].strip()


    creative_direction = ""
    intent = project.author_intent
    style = project.writing_style
    if task == TaskType.GENERATE_OUTLINE:
        parts = []
        intent_frag = intent.to_prompt_fragment()
        style_frag = style.to_prompt_fragment()
        if intent_frag:
            parts.append(intent_frag)
        if style_frag:
            parts.append(style_frag)
        creative_direction = "\n".join(parts)
    elif task in (TaskType.WRITE_CHAPTER, TaskType.REWRITE_CHAPTER, TaskType.CHANGE_CHAPTER):
        parts = []
        style_frag = style.to_prompt_fragment()
        if style_frag:
            parts.append(style_frag)
        intent_chapter_lines = []
        if intent.emotional_journey:
            intent_chapter_lines.append(
                f"Reader's emotional experience to sustain: {intent.emotional_journey}"
            )
        if intent.avoid:
            intent_chapter_lines.append(f"Avoid entirely: {intent.avoid}")
        if intent_chapter_lines:
            parts.append("\n".join(intent_chapter_lines))
        creative_direction = "\n".join(parts)
    elif task == TaskType.REVIEW_CHAPTER:
        intent_frag = intent.to_prompt_fragment()
        style_frag = style.to_prompt_fragment()
        parts = []
        if intent_frag:
            parts.append(intent_frag)
        if style_frag:
            parts.append(style_frag)
        creative_direction = "\n".join(parts)

    return {
        "Base": base,
        "Language": language,
        "Task Instructions": _estimate_task_instruction(task),
        "Synopsis": synopsis,
        "Characters": characters,
        "Outline": outline,
        "World": world,
        "Memory": memory,
        "Chat Summary": chat_summary,
        "Creative Direction": creative_direction,
        "Author Instructions": author_instructions,
    }


def _compact_sections(
    sections: dict[str, str],
    max_context_tokens: int,
    task: TaskType,
    project: Project,
) -> dict[str, str]:
    budgets = {
        "Base": max(40, max_context_tokens // 80),
        "Language": max(12, max_context_tokens // 400),
        "Task Instructions": max(80, max_context_tokens // 32),
        "Synopsis": max(120, max_context_tokens // 20),
        "Characters": max(180, max_context_tokens // 14),
        "Outline": max(350, max_context_tokens // 8),

        "World": max(80, max_context_tokens // 32),
        "Memory": max(150, max_context_tokens // 16),
        "Chat Summary": max(100, max_context_tokens // 24),

        "Creative Direction": max(60, max_context_tokens // 20),
        "Author Instructions": max(80, max_context_tokens // 40),
    }
    if task == TaskType.WRITE_CHAPTER:
        budgets["Memory"] = max(budgets["Memory"], max_context_tokens // 10)
        budgets["Chat Summary"] = max(budgets["Chat Summary"], max_context_tokens // 20)







        sections["Outline"] = ""
    if task in (TaskType.CHAT, TaskType.REVIEW_CHAPTER):
        budgets["Chat Summary"] = max(budgets["Chat Summary"], max_context_tokens // 12)
        budgets["Memory"] = max(budgets["Memory"], max_context_tokens // 12)
    if len(project.characters) > 12:
        sections["Characters"] = format_characters_block(project.characters[:12])

    for name, text in list(sections.items()):
        sections[name] = _truncate_to_budget(text, budgets.get(name, 0))
    return sections


def _repair_alternation(messages: list[dict]) -> list[dict]:
    """
    Ensure messages strictly alternate user/assistant starting with "user".
    Token trimming can leave a dangling assistant reply; most llama.cpp
    chat templates require strict alternation and raise a jinja error otherwise.
    """

    while messages and messages[0]["role"] != "user":
        messages.pop(0)


    repaired: list[dict] = []
    for m in messages:
        if repaired and repaired[-1]["role"] == m["role"]:
            repaired[-1] = {
                "role": m["role"],
                "content": repaired[-1]["content"] + "\n\n" + m["content"],
            }
        else:
            repaired.append(dict(m))
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
    """
    Build the message list to send to the model.
    Layout: [system] + recent non-summarized messages + [user].
    include_story_context=False omits all story sections (useful for plain chat).
    """
    sections = _compact_sections(
        _section_payloads(project, task, system_prompt),
        max_context_tokens,
        task,
        project,
    )
    system_content_parts = [sections["Base"]]
    if sections["Language"]:
        system_content_parts.append(sections["Language"])
    if sections["Task Instructions"]:
        system_content_parts.append(sections["Task Instructions"])
    if include_story_context:
        if sections["Synopsis"]:
            system_content_parts.append(f"\n\n{sections['Synopsis']}")
        if sections["Characters"]:
            system_content_parts.append(f"\n\n## Established Characters\n{sections['Characters']}")
        if sections["Outline"]:
            system_content_parts.append(f"\n\n## Outline\n{sections['Outline']}")
        if sections["World"]:
            system_content_parts.append(f"\n\n## World & Setting\n{sections['World']}")
        if sections["Memory"]:
            system_content_parts.append(f"\n\n## Story Memory\n{sections['Memory']}")
        if sections["Chat Summary"]:
            system_content_parts.append(f"\n\n## Conversation Summary (older messages)\n{sections['Chat Summary']}")
        if sections.get("Creative Direction"):
            system_content_parts.append(f"\n\n## Author's Creative Direction\n{sections['Creative Direction']}")
    if sections["Author Instructions"]:
        system_content_parts.append(f"\n\n## Additional Author Instructions\n{sections['Author Instructions']}")
    system_content = "\n".join(system_content_parts)


    eligible = [
        m for m in project.chat_messages
        if m.role in (MessageRole.USER, MessageRole.ASSISTANT)
        and not m.summarized
    ]

    window = eligible[-project.recent_message_window:]

    recent_msgs = [
        {"role": m.role.value, "content": m.content}
        for m in window
    ]

    system_tokens = _estimate_tokens(system_content)
    user_tokens = _estimate_tokens(user_message)
    history_tokens = _messages_token_count(recent_msgs)
    reply_headroom = reply_reserved if reply_reserved is not None else max(256, min(4096, max_context_tokens // 3))
    total = system_tokens + user_tokens + history_tokens + reply_headroom

    while total > max_context_tokens and len(recent_msgs) > 1:
        removed = recent_msgs.pop(0)
        total -= _estimate_tokens(removed["content"])


    recent_msgs = _repair_alternation(recent_msgs)


    if recent_msgs and recent_msgs[-1]["role"] == "user":
        recent_msgs[-1] = {
            "role": "user",
            "content": recent_msgs[-1]["content"] + "\n\n" + user_message,
        }
        trailing_user_msg = None
    else:
        trailing_user_msg = {"role": "user", "content": user_message}

    messages: list[dict] = [{"role": "system", "content": system_content}]
    messages.extend(recent_msgs)
    if trailing_user_msg is not None:
        messages.append(trailing_user_msg)


    reserved = reply_reserved if reply_reserved is not None else max(256, min(4096, max_context_tokens // 3))
    non_user_tokens = _messages_token_count(messages[:-1]) if trailing_user_msg is not None else _messages_token_count(messages)
    target_msg = messages[-1] if trailing_user_msg is not None else (messages[-1] if messages[-1]["role"] == "user" else None)
    if target_msg is not None:
        budget_for_msg = max(200, max_context_tokens - non_user_tokens - reserved - 64)
        if _estimate_tokens(target_msg["content"]) > budget_for_msg:
            approx_chars = max(200, budget_for_msg * 4)
            content = target_msg["content"]
            if len(content) > approx_chars:
                target_msg["content"] = (
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
    """
    Build a lean context for chapter review.

    Review tasks should not inherit chat history or the full writing context.
    They only need the system instructions, the chapter text, and any compact
    project state that helps judge the chapter.
    """

    _review_intent_frag = project.author_intent.to_prompt_fragment()
    _review_style_frag = project.writing_style.to_prompt_fragment()
    _review_creative = "\n".join(
        p for p in [_review_intent_frag, _review_style_frag] if p
    )

    sections = _compact_sections(
        {
            "Base": (
                f"You are an AI assistant helping to write a novel titled '{project.title}'. "
                "You are deeply familiar with the story world, characters, and plot. "
                "You respond only in the context of this story."
            ),
            "Language": "",
            "Task Instructions": _estimate_task_instruction(TaskType.REVIEW_CHAPTER),
            "Synopsis": f"Story Synopsis:\n{project.synopsis.strip()}" if project.synopsis.strip() else "",
            "Characters": format_characters_block(project.characters),
            "Outline": "",
            "World": "",
            "Memory": "",
            "Chat Summary": "",
            "Creative Direction": _review_creative,
            "Author Instructions": "",
        },
        max_context_tokens,
        TaskType.REVIEW_CHAPTER,
        project,
    )


    chapter_num = project.current_chapter if project.current_chapter else max(1, len(project.chapters))
    specific_outline = extract_outline_section(project.outline, chapter_num)
    if specific_outline:
        sections["Outline"] = specific_outline

    system_content_parts = [sections["Base"]]
    if sections["Task Instructions"]:
        system_content_parts.append(sections["Task Instructions"])
    if sections["Synopsis"]:
        system_content_parts.append(f"\n\n{sections['Synopsis']}")
    if sections["Characters"]:
        system_content_parts.append(f"\n\n## Established Characters\n{sections['Characters']}")
    if sections["Outline"]:
        system_content_parts.append(f"\n\n## Outline\n{sections['Outline']}")
    if sections.get("Creative Direction"):
        system_content_parts.append(f"\n\n## Author's Creative Direction\n{sections['Creative Direction']}")
    if sections["Author Instructions"]:
        system_content_parts.append(f"\n\n## Additional Author Instructions\n{sections['Author Instructions']}")
    system_content = "\n".join(system_content_parts)



    reserved = reply_reserved if reply_reserved is not None else 512
    system_tokens = _section_tokens(system_content)
    budget_for_user = max(200, max_context_tokens - system_tokens - reserved - 64)
    if _estimate_tokens(user_message) > budget_for_user:
        approx_chars = max(200, budget_for_user * 4)
        if len(user_message) > approx_chars:
            user_message = (
                "[... earlier content truncated to fit context budget ...]\n\n"
                + user_message[-approx_chars:].lstrip()
            )

    messages: list[dict] = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_message},
    ]
    return messages


def estimate_context_usage(
    project: Project,
    user_message: str,
    system_prompt: str,
    max_context_tokens: int = 3200,
    task: TaskType = TaskType.CHAT,
    reply_reserved: Optional[int] = None,
    requested_max_tokens: Optional[int] = None,
) -> dict:
    """
    Estimate how much of the context window will be consumed before generation.

    The numbers are approximate, but they are good enough for console logging
    and for spotting when a prompt is getting too close to the model limit.
    """
    sections = _compact_sections(
        _section_payloads(project, task, system_prompt),
        max_context_tokens,
        task,
        project,
    )
    system_content_parts = [sections["Base"]]
    if sections["Language"]:
        system_content_parts.append(sections["Language"])
    if sections["Task Instructions"]:
        system_content_parts.append(sections["Task Instructions"])
    if sections["Synopsis"]:
        system_content_parts.append(f"\n\n{sections['Synopsis']}")
    if sections["Characters"]:
        system_content_parts.append(f"\n\n## Established Characters\n{sections['Characters']}")
    if sections["Outline"]:
        system_content_parts.append(f"\n\n## Outline\n{sections['Outline']}")
    if sections["World"]:
        system_content_parts.append(f"\n\n## World & Setting\n{sections['World']}")
    if sections["Memory"]:
        system_content_parts.append(f"\n\n## Story Memory\n{sections['Memory']}")
    if sections["Chat Summary"]:
        system_content_parts.append(f"\n\n## Conversation Summary (older messages)\n{sections['Chat Summary']}")
    if sections.get("Creative Direction"):
        system_content_parts.append(f"\n\n## Author's Creative Direction\n{sections['Creative Direction']}")
    if sections["Author Instructions"]:
        system_content_parts.append(f"\n\n## Additional Author Instructions\n{sections['Author Instructions']}")
    system_content = "\n".join(system_content_parts)

    eligible = [
        m for m in project.chat_messages
        if m.role in (MessageRole.USER, MessageRole.ASSISTANT)
        and not m.summarized
    ]
    window = eligible[-project.recent_message_window:]
    recent_msgs = [{"role": m.role.value, "content": m.content} for m in window]

    system_tokens = _estimate_tokens(system_content)
    user_tokens = _estimate_tokens(user_message)
    history_tokens = _messages_token_count(recent_msgs)
    prompt_tokens = system_tokens + user_tokens + history_tokens
    available_reply_tokens = max(0, max_context_tokens - prompt_tokens)
    reply_headroom = reply_reserved if reply_reserved is not None else max(256, min(4096, max_context_tokens // 3))
    requested_reply = requested_max_tokens if requested_max_tokens is not None else reply_headroom
    effective_max_tokens = min(requested_reply, available_reply_tokens)
    estimated_total = prompt_tokens + effective_max_tokens

    section_report = {name: _section_tokens(text) for name, text in sections.items()}

    return {
        "sections": section_report,
        "prompt_tokens": prompt_tokens,
        "system_tokens": system_tokens,
        "user_tokens": user_tokens,
        "history_tokens": history_tokens,
        "available_reply_tokens": available_reply_tokens,
        "reply_headroom": reply_headroom,
        "requested_max_tokens": requested_reply,
        "effective_max_tokens": effective_max_tokens,
        "estimated_total": estimated_total,
        "estimated_remaining": max(0, available_reply_tokens - effective_max_tokens),
        "max_context_tokens": max_context_tokens,
        "recent_messages": len(recent_msgs),
    }


def estimate_messages_tokens(messages: list[dict]) -> int:
    """Estimate prompt tokens for a finalized message list."""
    return _messages_token_count(messages)


def should_summarize(project: Project, threshold: int = 30) -> bool:
    """Return True when unsummarized messages exceed the threshold."""
    unsummarized = [
        m for m in project.chat_messages
        if m.role in (MessageRole.USER, MessageRole.ASSISTANT)
        and not m.summarized
    ]
    return len(unsummarized) > threshold + project.recent_message_window


def mark_old_messages_summarized(project: Project) -> list[ChatMessage]:
    """
    Mark messages outside the recent window as summarized.
    Returns the list of messages that were just marked.
    """
    eligible = [
        m for m in project.chat_messages
        if m.role in (MessageRole.USER, MessageRole.ASSISTANT)
        and not m.summarized
    ]

    to_summarize = eligible[: max(0, len(eligible) - project.recent_message_window)]
    for m in to_summarize:
        m.summarized = True
    return to_summarize


def build_summarization_prompt(messages_to_summarize: list[ChatMessage]) -> str:
    """Build a prompt that asks the model to summarize old conversation messages."""
    history_text = "\n".join(
        f"{m.role.value.upper()}: {m.content}"
        for m in messages_to_summarize
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
    """
    Build the system prompt for the given task.

    custom_instructions: appended after task-specific instructions (always has final word).
    language: if set, an explicit language instruction overrides model bias toward English.
    allow_nsfw: enables adult content for creative tasks only (no effect on review/chat/summary).
    """

    _CREATIVE_TASKS = {
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


    if allow_nsfw and task in _CREATIVE_TASKS:
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
        prompt += f"\n\n## Additional Author Instructions\n{custom_instructions.strip()}"

    return prompt