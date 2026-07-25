"""
Memory Agent.

Builds prompts to update and maintain Story Memory.
"""

from __future__ import annotations

from engine.models import Chapter, Project


class MemoryAgent:

    def build_memory_update_prompt(self, project: Project, chapter: Chapter) -> str:
        existing = (
            f"\nExisting Story Memory:\n{project.memory}\n"
            if project.memory
            else "\nNo existing story memory.\n"
        )
        return (
            f"Update the story memory after Chapter {chapter.number}: '{chapter.title}'.\n"
            f"{existing}\n"
            f"New chapter:\n{chapter.content}\n\n"
            "Extract and record in a structured format:\n"
            "## Characters\n"
            "- Any new characters introduced\n"
            "- Important character developments\n\n"
            "## Plot Events\n"
            "- Key events that occurred\n"
            "- Decisions made by characters\n\n"
            "## World & Setting\n"
            "- New locations or world details established\n\n"
            "## Foreshadowing & Setup\n"
            "- Any hints or setups planted for future chapters\n\n"
            "## Open Threads\n"
            "- Unresolved plot threads\n\n"
            "Preserve all existing memory. Add new information. Do not remove old entries."
        )

    def build_character_extraction_prompt(self, text: str) -> str:
        return (
            "Extract all characters mentioned in the following text. "
            "For each character provide:\n"
            "- Name\n"
            "- Role (protagonist/antagonist/supporting/minor)\n"
            "- Brief description\n\n"
            f"Text:\n{text}\n\n"
            "Return as a structured list."
        )

    def build_world_extraction_prompt(self, text: str) -> str:
        return (
            "Extract world-building information from the following text:\n"
            "- Locations and their descriptions\n"
            "- Rules or systems (magic, technology, social)\n"
            "- Historical facts mentioned\n"
            "- Cultural details\n\n"
            f"Text:\n{text}\n\n"
            "Return as a structured summary."
        )
