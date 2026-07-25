"""
Reviewer Agent.

Builds prompts for review and quality-check tasks.
"""

from __future__ import annotations

from engine.models import Chapter, Project


class ReviewerAgent:

    def build_chapter_review_prompt(self, project: Project, chapter: Chapter) -> str:
        context = ""
        if project.characters:
            chars = "\n".join(
                f"- {c.name} ({c.role}): {c.description}" for c in project.characters
            )
            context += f"\nEstablished characters:\n{chars}"

        if project.memory:
            context += f"\nStory memory:\n{project.memory}"

        return (
            f"Review Chapter {chapter.number}: '{chapter.title}' from '{project.title}'."
            f"{context}\n\n"
            f"Chapter content:\n{chapter.content}\n\n"
            "Provide a detailed review covering:\n"
            "1. Prose quality and readability\n"
            "2. Character consistency\n"
            "3. Pacing and scene structure\n"
            "4. Dialogue authenticity\n"
            "5. Continuity with established story facts\n"
            "6. Specific suggestions for improvement\n\n"
            "Be specific and constructive."
        )

    def build_outline_review_prompt(self, project: Project) -> str:
        return (
            f"Review the following outline for '{project.title}'.\n"
            + (f"\nSynopsis:\n{project.synopsis}\n" if project.synopsis else "")
            + f"\nOutline:\n{project.outline}\n\n"
            "Analyze for:\n"
            "1. Story structure (three-act structure or equivalent)\n"
            "2. Pacing across chapters\n"
            "3. Character arc development\n"
            "4. Plot coherence and internal logic\n"
            "5. Missing scenes or transitions\n"
            "6. Climax and resolution quality\n\n"
            "Provide specific, actionable feedback."
        )

    def build_continuity_check_prompt(
        self, project: Project, new_chapter: Chapter
    ) -> str:
        facts = []
        if project.characters:
            for c in project.characters:
                facts.append(f"- {c.name}: {c.description}")

        return (
            f"Check Chapter {new_chapter.number} for continuity errors.\n\n"
            + ("Established facts:\n" + "\n".join(facts) + "\n\n" if facts else "")
            + (f"Story memory:\n{project.memory}\n\n" if project.memory else "")
            + f"New chapter:\n{new_chapter.content}\n\n"
            "List any continuity errors or inconsistencies found. "
            "If none found, say 'No continuity errors detected.'"
        )
