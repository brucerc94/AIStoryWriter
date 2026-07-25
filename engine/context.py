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

from typing import Optional

from engine.models import ChatMessage, MessageRole, Project, TaskType


# Approximate token estimate: 1 token ≈ 4 chars
def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _messages_token_count(messages: list[dict]) -> int:
    total = 0
    for m in messages:
        total += _estimate_tokens(m.get("content", ""))
    return total


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
) -> list[dict]:
    """
    Build the list of messages to send to the model.

    Layout:
      [system]           ← system prompt + story memory + summary
      [assistant/user …] ← recent non-summarized messages
      [user]             ← current user message
    """
    # Build system content
    system_parts = [system_prompt.strip()]

    if project.memory.strip():
        system_parts.append(
            "\n\n## Story Memory\n" + project.memory.strip()
        )

    if project.chat_summary.strip():
        system_parts.append(
            "\n\n## Conversation Summary (older messages)\n" + project.chat_summary.strip()
        )

    system_content = "\n".join(system_parts)

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

    total = system_tokens + user_tokens + history_tokens + 512  # 512 for reply headroom

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
    project: Project, task: TaskType, custom_instructions: str = ""
) -> str:
    """
    Return an appropriate system prompt for the given task.

    custom_instructions: optional author-provided text (from Settings)
    appended at the very end, after the task-specific instructions, so it
    always has the final word — useful for persistent style/content rules
    that should apply no matter which task is running.
    """
    base = (
        f"You are an AI assistant helping to write a novel titled '{project.title}'. "
        "You are deeply familiar with the story world, characters, and plot. "
        "You respond only in the context of this story."
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
            "Write a complete outline with enough chapters to tell the full story."
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
    }

    instruction = task_instructions.get(task, "")
    if project.synopsis:
        base += f"\n\nStory Synopsis:\n{project.synopsis}"

    prompt = f"{base}\n\n{instruction}".strip()

    if custom_instructions and custom_instructions.strip():
        prompt += f"\n\n## Additional Author Instructions\n{custom_instructions.strip()}"

    return prompt
