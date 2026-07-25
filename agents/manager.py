"""
Manager Agent.

Coordinates workflow execution. Decides which model to use for each task.
Does not talk to the user directly — only the WorkflowThread does.
"""

from __future__ import annotations

from engine.models import Project, TaskType


class ManagerAgent:
    """
    Selects the appropriate model path for each task type,
    falling back gracefully when no assignment exists.
    """

    def get_model_for_task(self, project: Project, task: TaskType) -> str:
        """
        Returns the model path assigned to the given task.
        Falls back to the chat model if nothing is assigned.
        Falls back to any assigned model as last resort.
        """
        ma = project.model_assignments
        model = ma.get(task)
        if model:
            return model

        # Fallback priority
        fallback_order = [
            TaskType.CHAT,
            TaskType.WRITE_CHAPTER,
            TaskType.GENERATE_OUTLINE,
            TaskType.WRITE_SYNOPSIS,
            TaskType.REVIEW_CHAPTER,
            TaskType.REVIEW_OUTLINE,
            TaskType.UPDATE_MEMORY,
            TaskType.CONVERSATION_SUMMARY,
        ]
        for ft in fallback_order:
            m = ma.get(ft)
            if m:
                return m

        return ""

    def suggest_workflow_next_step(self, project: Project) -> tuple[TaskType, str]:
        """
        Analyzes the current project state and suggests the next logical step.
        Returns (task_type, human_readable_description).
        """
        if not project.synopsis:
            return TaskType.WRITE_SYNOPSIS, "Write the story synopsis"

        if not project.outline:
            return TaskType.GENERATE_OUTLINE, "Generate the chapter outline"

        # Check if there are unreviewed chapters
        for ch in project.chapters:
            if not ch.reviewed:
                return TaskType.REVIEW_CHAPTER, f"Review Chapter {ch.number}"

        # Next chapter to write
        next_num = len(project.chapters) + 1
        return TaskType.WRITE_CHAPTER, f"Write Chapter {next_num}"
