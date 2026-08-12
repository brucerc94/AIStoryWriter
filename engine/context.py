"""
Context Manager.

Responsible for:
- Building the message list sent to the model
- Never deleting messages (only marking them summarized)
- Summarizing old messages when context is full
- Always injecting: system prompt, story memory, summary, recent messages

The model never sees all messages — only recent ones.
The UI always shows all messages.
"""

from __future__ import annotations

import logging
from typing import Optional

from engine.models import ChatMessage, MessageRole, Project, TaskType

logger = logging.getLogger("context")


# Approximate token estimate: 1 token ≈ 4 chars
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


def _extract_outline_section(outline: str, chapter_number: int) -> str:
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
    task_instructions = {
        TaskType.CHAT: (
            "Engage naturally with the author. Help them develop their story, answer questions, "
            "brainstorm ideas, and provide creative suggestions. Be concise and helpful."
        ),
        TaskType.WRITE_SYNOPSIS: (
            "Write a compelling 5-10 paragraph synopsis for this novel. "
            "Cover the main premise, central conflict, and emotional core. "
            "Write only the synopsis text."
        ),
        TaskType.GENERATE_OUTLINE: (
            "Generate a detailed chapter-by-chapter outline for this novel. "
            "For each chapter, use a structured format with these sections:\n"
            "## Chapter N: Title\n"
            "Objective:\n"
            "1-2 sentences describing the chapter's purpose.\n"
            "Scenes required:\n"
            "- Bullet the beats that must happen in this chapter.\n"
            "Scenes prohibited:\n"
            "- Bullet anything that must not happen yet.\n\n"
            "Keep the outline concrete and binding. "
            "Do not turn it into a free-form summary. "
            "If the user's request specifies an exact number of chapters, "
            "produce precisely that many, numbered sequentially starting at 1 "
            "— do not stop early or pad with extra chapters. If no number is "
            "given, use enough chapters to tell the full story. "
            "When you mention or generate characters, their description must stay purely visual: "
            "age, overall appearance, visible physical traits, notable scars/features, and clothing/style. "
            "Do not include personality, backstory, secrets, memory, or motivations in the description."
        ),
        TaskType.REVIEW_OUTLINE: (
            "Review the provided story outline. Analyze it for:\n"
            "- Plot coherence and pacing\n"
            "- Character arc consistency\n"
            "- Story structure (setup, rising action, climax, resolution)\n"
            "- Missing scenes or transitions\n"
            "Provide specific, actionable feedback. Then suggest an improved version if needed."
        ),
        TaskType.WRITE_CHAPTER: (
            "Write this chapter of the novel. "
            "Match the established tone, voice, and style. "
            "Write vivid prose with dialogue, description, and action. "
            "Do not summarize — write the full scene. "
            "Write only the chapter content."
        ),
        TaskType.REVIEW_CHAPTER: (
            "Review this chapter. Check for:\n"
            "- Consistency with established characters and world\n"
            "- Prose quality and readability\n"
            "- Pacing and scene structure\n"
            "- Dialogue authenticity\n"
            "- Continuity errors\n"
            "Give specific feedback and suggest improvements."
        ),
        TaskType.REWRITE_CHAPTER: (
            "Rewrite the chapter below to address the review feedback provided. "
            "Keep what's already working — voice, strong scenes, dialogue that "
            "lands — and fix specifically what the feedback flagged (pacing, "
            "continuity, prose issues, etc.). Write the full revised chapter "
            "content. Do not summarize the changes or add commentary — output "
            "only the rewritten chapter text."
        ),
        TaskType.UPDATE_MEMORY: (
            "Update the story memory file based on the latest chapter. "
            "Extract and record:\n"
            "- New character information revealed\n"
            "- Plot events that occurred\n"
            "- World-building details established\n"
            "- Foreshadowing or setups planted\n"
            "Write in a structured format. Preserve existing memory and add new entries."
        ),
        TaskType.CONVERSATION_SUMMARY: (
            "Summarize the provided conversation history for future reference. "
            "Be concise but complete. Capture all story decisions and context."
        ),
        TaskType.GENERATE_WORLD: (
            "Write detailed worldbuilding notes for this novel. Cover whichever "
            "of these are relevant to the story: geography and key locations, "
            "time period and technology level, magic or other special systems "
            "and their rules, political/social structures, culture and customs, "
            "and history relevant to the plot. Format as Markdown with headings. "
            "If world notes already exist below, add to and expand them rather "
            "than contradicting or repeating them."
        ),
    }
    return task_instructions.get(task, "")


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
    characters = "\n".join(
        f"- {c.name} ({c.role}): {c.description}" for c in project.characters
    ).strip()
    outline = project.outline.strip() if project.outline.strip() and task != TaskType.GENERATE_OUTLINE else ""
    world = project.world.strip()
    memory = project.memory.strip()
    chat_summary = project.chat_summary.strip()
    author_instructions = ""
    if "## Additional Author Instructions" in system_prompt:
        author_instructions = system_prompt.split("## Additional Author Instructions", 1)[1].strip()

    # Build the Author's Creative Direction section from the two profile objects.
    # Which fragments to include depends on the task:
    #   - GENERATE_OUTLINE  : full intent + full style (shapes the whole structure)
    #   - WRITE_CHAPTER     : style only + emotional/avoid from intent (per-scene)
    #   - REWRITE_CHAPTER   : same as write — reviewer needs to know what to aim for
    #   - REVIEW_CHAPTER    : full intent so the reviewer can judge against the goal
    #   - everything else   : omit (chat, memory, summary don't benefit from it)
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
    elif task in (TaskType.WRITE_CHAPTER, TaskType.REWRITE_CHAPTER):
        # For chapter writing: style always useful; from intent only the
        # emotional target and "avoid" list (themes/inspirations are outline-time).
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
        # Reviewer needs the full intent to judge against the book's purpose.
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
        "World": max(120, max_context_tokens // 20),
        "Memory": max(150, max_context_tokens // 16),
        "Chat Summary": max(100, max_context_tokens // 24),
        # Creative Direction is deliberately capped low: it should be a
        # handful of concise directives, never a prose essay. ~200 tokens
        # covers all 8 fields filled in with one-sentence answers.
        "Creative Direction": max(60, max_context_tokens // 20),
        "Author Instructions": max(80, max_context_tokens // 40),
    }
    if task == TaskType.WRITE_CHAPTER:
        budgets["Outline"] = max(budgets["Outline"], max_context_tokens // 5)
        budgets["Memory"] = max(budgets["Memory"], max_context_tokens // 10)
        budgets["Chat Summary"] = max(budgets["Chat Summary"], max_context_tokens // 20)
        chapter_num = project.current_chapter + 1 if project.current_chapter else max(1, len(project.chapters) + 1)
        specific = _extract_outline_section(project.outline, chapter_num)
        if specific:
            sections["Outline"] = specific
    if task in (TaskType.CHAT, TaskType.REVIEW_CHAPTER):
        budgets["Chat Summary"] = max(budgets["Chat Summary"], max_context_tokens // 12)
        budgets["Memory"] = max(budgets["Memory"], max_context_tokens // 12)
    if len(project.characters) > 12:
        sections["Characters"] = "\n".join(
            f"- {c.name} ({c.role}): {c.description}" for c in project.characters[:12]
        )

    for name, text in list(sections.items()):
        sections[name] = _truncate_to_budget(text, budgets.get(name, 0))
    return sections


def _repair_alternation(messages: list[dict]) -> list[dict]:
    """
    Guarantee the list strictly alternates user/assistant/user/assistant…
    and starts with "user".

    Trimming old messages to fit the token budget removes them one at a
    time from the front, which can leave a dangling "assistant" reply
    whose paired "user" prompt got trimmed away. Most llama.cpp chat
    templates hard-require strict alternation (with the conversation,
    excluding the optional system message, starting on "user") and raise
    a jinja ValueError otherwise — so repair the window before it's ever
    sent to the model rather than trusting it's already well-formed.
    """
    # A window can't start with a lone assistant reply — drop it, and any
    # further leading assistant turns, until it starts with "user".
    while messages and messages[0]["role"] != "user":
        messages.pop(0)

    # Merge any remaining consecutive same-role turns instead of dropping
    # content, so nothing the user typed or the model wrote is lost.
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
) -> list[dict]:
    """
    Build the list of messages to send to the model.

    Layout:
      [system]           ← system prompt + story memory + summary
      [assistant/user …] ← recent non-summarized messages
      [user]             ← current user message
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

    # Gather recent non-summarized messages (excluding system/summary roles)
    eligible = [
        m for m in project.chat_messages
        if m.role in (MessageRole.USER, MessageRole.ASSISTANT)
        and not m.summarized
    ]

    # Take the most recent window
    window = eligible[-project.recent_message_window:]

    # Build tentative message list
    recent_msgs = [
        {"role": m.role.value, "content": m.content}
        for m in window
    ]

    # Estimate total tokens
    system_tokens = _estimate_tokens(system_content)
    user_tokens = _estimate_tokens(user_message)
    history_tokens = _messages_token_count(recent_msgs)
    reply_headroom = reply_reserved if reply_reserved is not None else max(256, min(4096, max_context_tokens // 3))
    total = system_tokens + user_tokens + history_tokens + reply_headroom

    # If over budget, trim from the oldest of the window
    while total > max_context_tokens and len(recent_msgs) > 1:
        removed = recent_msgs.pop(0)
        total -= _estimate_tokens(removed["content"])

    # Trimming above can break strict user/assistant alternation — fix it
    # before this ever reaches the model.
    recent_msgs = _repair_alternation(recent_msgs)

    # If the repaired window's last turn is already "user" (e.g. the
    # trailing assistant reply was merged/dropped), fold the new user
    # message into it instead of producing back-to-back "user" turns.
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
    # Build creative direction for review: full intent + style so the
    # reviewer can judge the chapter against the author's stated goals.
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
            "Characters": "\n".join(
                f"- {c.name} ({c.role}): {c.description}" for c in project.characters
            ).strip(),
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

    # For review we keep only the specific chapter entry if we can extract it.
    chapter_num = project.current_chapter if project.current_chapter else max(1, len(project.chapters))
    specific_outline = _extract_outline_section(project.outline, chapter_num)
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
    """
    Returns True when there are enough old unsummarized messages
    that we should trigger summarization.
    """
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
    # Keep the recent window, summarize the rest
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
    Return an appropriate system prompt for the given task.

    custom_instructions: optional author-provided text (from Settings)
    appended at the very end, after the task-specific instructions, so it
    always has the final word — useful for persistent style/content rules
    that should apply no matter which task is running.

    language: if set, an explicit instruction to respond in that language
    is placed right up front. Every built-in instruction here is written
    in English, which otherwise biases local models toward answering in
    English even when the story/characters/everything else is in another
    language — an explicit instruction is the reliable fix, not hoping a
    translated scaffold alone will carry the model's output language.

    allow_nsfw: when True, the model receives an explicit content-freedom
    instruction for creative-writing tasks (synopsis, outline, chapters,
    rewrite, world, memory, characters). Has no effect on review, chat,
    or summary tasks — those do not produce creative content.
    """
    # Tasks that generate creative prose — the only ones where NSFW freedom
    # meaningfully changes what the model writes.
    _CREATIVE_TASKS = {
        TaskType.WRITE_SYNOPSIS,
        TaskType.GENERATE_OUTLINE,
        TaskType.WRITE_CHAPTER,
        TaskType.REWRITE_CHAPTER,
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

    # NSFW instruction — placed immediately after base identity so it has
    # maximum authority over the generation. Phrased as an explicit permission
    # grant rather than a style note, because local models respond better to
    # "you are allowed" than "the author prefers". Only injected for tasks
    # that produce creative content.
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

    task_instructions = {
        TaskType.CHAT: (
            "Engage naturally with the author. Help them develop their story, answer questions, "
            "brainstorm ideas, and provide creative suggestions. Be concise and helpful."
        ),
        TaskType.WRITE_SYNOPSIS: (
            "Write a compelling 2-3 paragraph synopsis for this novel. "
            "Cover the main premise, central conflict, and emotional core. "
            "Write only the synopsis text."
        ),
        TaskType.GENERATE_OUTLINE: (
            "Generate a detailed chapter-by-chapter outline for this novel. "
            "For each chapter: provide the chapter number, a title, and a 2-3 sentence summary. "
            "Format each chapter as:\n"
            "## Chapter N: Title\n"
            "Summary text.\n\n"
            "Write a complete outline with enough chapters to tell the full story. "
            "When you mention or generate characters, their description must stay purely visual: "
            "age, overall appearance, visible physical traits, notable scars/features, and clothing/style. "
            "Do not include personality, backstory, secrets, memory, or motivations in the description."
        ),
        TaskType.REVIEW_OUTLINE: (
            "Review the provided story outline. Analyze it for:\n"
            "- Plot coherence and pacing\n"
            "- Character arc consistency\n"
            "- Story structure (setup, rising action, climax, resolution)\n"
            "- Missing scenes or transitions\n"
            "Provide specific, actionable feedback. Then suggest an improved version if needed."
        ),
        TaskType.WRITE_CHAPTER: (
            "Write this chapter strictly from the outline. "
            "The outline is binding and is the source of truth. "
            "Develop only the scenes, beats, and outcomes that the outline specifies. "
            "Do not invent major events, resolutions, villains, revelations, or new plot turns that are not explicitly present in the outline. "
            "Do not advance events from later chapters. "
            "Do not resolve conflicts unless the outline resolves them here. "
            "Do not introduce major characters before the outline introduces them. "
            "If the outline leaves a situation open, leave the chapter open as well. "
            "Match the established tone, voice, and style while staying faithful to the outline. "
            "Write only the chapter content."
        ),
        TaskType.REVIEW_CHAPTER: (
            "Review this chapter. Check for:\n"
            "- Consistency with established characters and world\n"
            "- Prose quality and readability\n"
            "- Pacing and scene structure\n"
            "- Dialogue authenticity\n"
            "- Continuity errors\n"
            "Give specific feedback and suggest improvements."
        ),
        TaskType.REWRITE_CHAPTER: (
            "Rewrite the chapter below to address the review feedback provided. "
            "Keep what's already working — voice, strong scenes, dialogue that "
            "lands — and fix specifically what the feedback flagged (pacing, "
            "continuity, prose issues, etc.). Write the full revised chapter "
            "content. Do not summarize the changes or add commentary — output "
            "only the rewritten chapter text."
        ),
        TaskType.UPDATE_MEMORY: (
            "Update the story memory file based on the latest chapter. "
            "Extract and record:\n"
            "- New character information revealed\n"
            "- Plot events that occurred\n"
            "- World-building details established\n"
            "- Foreshadowing or setups planted\n"
            "Write in a structured format. Preserve existing memory and add new entries."
        ),
        TaskType.CONVERSATION_SUMMARY: (
            "Summarize the provided conversation history for future reference. "
            "Be concise but complete. Capture all story decisions and context."
        ),
        TaskType.GENERATE_WORLD: (
            "Write detailed worldbuilding notes for this novel. Cover whichever "
            "of these are relevant to the story: geography and key locations, "
            "time period and technology level, magic or other special systems "
            "and their rules, political/social structures, culture and customs, "
            "and history relevant to the plot. Format as Markdown with headings. "
            "If world notes already exist below, add to and expand them rather "
            "than contradicting or repeating them."
        ),
    }

    instruction = task_instructions.get(task, "")
    if project.synopsis:
        base += f"\n\nStory Synopsis:\n{project.synopsis}"

    # Include the author's creative direction in synopsis generation so the
    # initial pitch matches the intended emotional arc, themes, and style.
    intent_fragment = project.author_intent.to_prompt_fragment()
    style_fragment = project.writing_style.to_prompt_fragment()
    creative_direction = "\n".join(part for part in [intent_fragment, style_fragment] if part)
    if creative_direction:
        base += f"\n\n## Author's Creative Direction\n{creative_direction}"

    if project.characters:
        chars = "\n".join(
            f"- {c.name} ({c.role}): {c.description}" for c in project.characters
        )
        base += f"\n\n## Established Characters\n{chars}"

    if project.outline and task != TaskType.GENERATE_OUTLINE:
        base += f"\n\n## Outline\n{project.outline}"

    if project.world:
        base += f"\n\n## World & Setting\n{project.world}"

    prompt = f"{base}\n\n{instruction}".strip()

    if custom_instructions and custom_instructions.strip():
        prompt += f"\n\n## Additional Author Instructions\n{custom_instructions.strip()}"

    return prompt
