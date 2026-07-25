"""
Writer Agent.

Builds specialized prompts for writing tasks.
Does not interact with the model directly — passes prompts to WorkflowThread.
"""

from __future__ import annotations

from engine.models import Chapter, Project


class WriterAgent:

    def build_chapter_prompt(
        self,
        project: Project,
        chapter_number: int,
        instructions: str = "",
    ) -> str:
        """Build a detailed prompt for writing a specific chapter."""
        parts = [f"Write Chapter {chapter_number} of the novel '{project.title}'."]

        if project.synopsis:
            parts.append(f"\nStory Synopsis:\n{project.synopsis}")

        if project.outline:
            # Extract just the relevant chapter section if possible
            relevant = self._extract_chapter_from_outline(project.outline, chapter_number)
            if relevant:
                parts.append(f"\nChapter outline:\n{relevant}")
            else:
                parts.append(f"\nFull outline:\n{project.outline}")

        if project.memory:
            parts.append(f"\nStory Memory (what has happened so far):\n{project.memory}")

        if project.characters:
            chars = "\n".join(
                f"- {c.name} ({c.role}): {c.description}" for c in project.characters
            )
            parts.append(f"\nCharacters:\n{chars}")

        if chapter_number > 1:
            prev = next(
                (c for c in project.chapters if c.number == chapter_number - 1), None
            )
            if prev and prev.content:
                # Include last ~500 chars of previous chapter for continuity
                tail = prev.content[-500:].strip()
                parts.append(f"\nEnd of previous chapter:\n...{tail}")

        if instructions:
            parts.append(f"\nSpecial instructions:\n{instructions}")

        parts.append(
            "\nWrite the full chapter with vivid prose, dialogue, and action. "
            "Do not summarize. Write only the chapter content."
        )

        return "\n\n".join(parts)

    def build_scene_continuation_prompt(
        self,
        project: Project,
        chapter: Chapter,
        continuation_hint: str = "",
    ) -> str:
        """Build a prompt to continue writing a scene mid-chapter."""
        tail = chapter.content[-800:].strip() if chapter.content else ""
        parts = [
            f"Continue writing Chapter {chapter.number}: '{chapter.title}' of '{project.title}'.",
        ]
        if tail:
            parts.append(f"\nThe chapter so far ends with:\n...{tail}")
        if continuation_hint:
            parts.append(f"\nContinue with: {continuation_hint}")
        parts.append("\nContinue seamlessly from where the text left off.")
        return "\n\n".join(parts)

    def build_rewrite_prompt(
        self,
        project: Project,
        original_text: str,
        instructions: str,
    ) -> str:
        """Build a prompt to rewrite a section with specific instructions."""
        return (
            f"Rewrite the following passage from '{project.title}' according to these instructions:\n"
            f"{instructions}\n\n"
            f"Original text:\n{original_text}\n\n"
            "Write only the rewritten text."
        )

    def _extract_chapter_from_outline(self, outline: str, chapter_number: int) -> str:
        """Try to extract only the relevant chapter section from the outline."""
        lines = outline.split("\n")
        capture = False
        result = []
        next_chapter = f"## Chapter {chapter_number + 1}"
        target = f"## Chapter {chapter_number}"

        for line in lines:
            if target in line:
                capture = True
            elif next_chapter in line and capture:
                break
            if capture:
                result.append(line)

        return "\n".join(result).strip()
