"""
Manager Agent.

Suggests the next logical workflow step based on project state (used by
the "What's next?" hint in the main window). Model-to-task assignment
itself lives in WorkflowWorker._load_model_for_task (engine/workflow.py) —
that is the single source of truth the actual generation calls go
through; this class does not duplicate it.
"""

from __future__ import annotations

from engine.models import Project, TaskType


class ManagerAgent:
    def suggest_workflow_next_step(self, project: Project) -> tuple[TaskType, str]:
        """
        Analyzes the current project state and suggests the next logical step.
        Returns (task_type, human_readable_description).
        """
        if not project.synopsis:
            return TaskType.WRITE_SYNOPSIS, "Write the story synopsis"

        if not project.outline:
            return TaskType.GENERATE_OUTLINE, "Generate the chapter outline"


        chapters = sorted(project.chapters, key=lambda c: c.number)

        # A chapter that exists but has no text yet must be written, not reviewed.
        unwritten = next((ch for ch in chapters if not (ch.content or "").strip()), None)
        if unwritten is not None:
            return TaskType.WRITE_CHAPTER, f"Write Chapter {unwritten.number}"

        for ch in chapters:
            if not ch.reviewed:
                return TaskType.REVIEW_CHAPTER, f"Review Chapter {ch.number}"

        # Numbering can have gaps after deletions or reordering, so continue
        # from the highest existing number instead of counting chapters.
        next_num = max((ch.number for ch in chapters), default=0) + 1
        return TaskType.WRITE_CHAPTER, f"Write Chapter {next_num}"
