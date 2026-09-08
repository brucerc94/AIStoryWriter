import unittest

from engine.context import build_context_for_model, build_system_prompt
from engine.models import ChatMessage, MessageRole, Project, TaskType


class ContextPromptIntegrityTests(unittest.TestCase):
    def test_full_task_prompt_is_preserved_for_outline_generation(self) -> None:
        project = Project(title="Test", synopsis="Original synopsis")
        task_text = "TASK_START\n" + ("format-rule " * 1000) + "\nTASK_END"
        system_prompt = task_text + "\n\n## Additional Author Instructions\nCUSTOM_END"
        messages = build_context_for_model(
            project,
            "Generate the outline.\n\nSynopsis: User supplied synopsis",
            system_prompt,
            max_context_tokens=4096,
            task=TaskType.GENERATE_OUTLINE,
            reply_reserved=512,
        )
        system = messages[0]["content"]
        self.assertIn("TASK_START", system)
        self.assertIn("TASK_END", system)
        self.assertIn("CUSTOM_END", system)
        self.assertNotIn("Original synopsis", system)

    def test_write_chapter_keeps_specific_outline_for_continuation(self) -> None:
        project = Project(
            title="Test",
            synopsis="Synopsis",
            current_chapter=1,
            outline=(
                "## Chapter 1: Opening\n"
                "Chapter Plan:\n"
                "This is a very detailed binding chapter plan.\n"
                "Continuity:\n"
                "The chapter ends at the gate.\n\n"
                "## Chapter 2: Next\n"
                "Chapter Plan:\nNext plan."
            ),
        )
        project.chat_messages = [
            ChatMessage(role=MessageRole.USER, content="old request"),
            ChatMessage(role=MessageRole.ASSISTANT, content="old response"),
        ]
        system_prompt = build_system_prompt(project, TaskType.WRITE_CHAPTER)
        messages = build_context_for_model(
            project,
            "Continue Chapter 1 from its current ending.",
            system_prompt,
            max_context_tokens=4096,
            task=TaskType.WRITE_CHAPTER,
            reply_reserved=512,
        )
        system = messages[0]["content"]
        self.assertIn("This is a very detailed binding chapter plan.", system)
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[-1]["role"], "user")

    def test_first_write_pass_does_not_duplicate_outline_into_system_context(self) -> None:
        project = Project(
            title="Test",
            current_chapter=1,
            outline="## Chapter 1: Opening\nChapter Plan:\nBinding plan here.\nContinuity:\nEnds here.",
        )
        system_prompt = build_system_prompt(project, TaskType.WRITE_CHAPTER)
        messages = build_context_for_model(
            project,
            "CHAPTER OUTLINE (binding)\nBinding plan here.",
            system_prompt,
            max_context_tokens=4096,
            task=TaskType.WRITE_CHAPTER,
            reply_reserved=512,
        )
        system = messages[0]["content"]
        self.assertNotIn("## Outline", system)
        self.assertIn("CHAPTER OUTLINE (binding)", messages[-1]["content"])


if __name__ == "__main__":
    unittest.main()
