import unittest

from engine.context import build_context_for_model, build_system_prompt
from engine.models import AuthorIntent, Character, ChatMode, MessageRole, Project, TaskType, WritingStyle


class DefineIdeaContextTests(unittest.TestCase):
    def test_define_idea_uses_relevant_characters_and_world(self) -> None:
        project = Project(
            title="Test Story",
            synopsis="Raul travels to a remote town after leaving home.",
            outline="## Chapter 1: The Journey\nRaul travels toward the town.",
            world=(
                "# World\n\n"
                "## Geography\n"
                "The road to the town crosses a narrow forest pass.\n\n"
                "## Culture & Customs\n"
                "Travelers are watched carefully at the town gate.\n\n"
                "## Relevant History\n"
                "Smugglers once used the forest pass for ambushes.\n"
            ),
        )
        project.characters = [
            Character(
                name="Juan",
                role="Traveler",
                description="An experienced fighter who knows the forest.",
                backstory="He has survived previous ambushes.",
            ),
            Character(
                name="Elena",
                role="Merchant",
                description="Runs a shop in the town.",
            ),
        ]
        project.author_intent = AuthorIntent(
            emotional_journey="Build unease before the arrival.",
            themes="Trust and uncertainty.",
        )
        project.writing_style = WritingStyle(
            narrator_pov="Third person limited",
            pacing="Tense and controlled",
            dialogue_style="Natural, terse dialogue with subtext.",
        )

        system_prompt = build_system_prompt(
            project,
            TaskType.CHAT,
            chat_mode=ChatMode.DEFINE_IDEA,
        )
        messages = build_context_for_model(
            project,
            "Quiero que Juan pase por algo horrible durante el viaje y llegue herido al pueblo.",
            system_prompt,
            max_context_tokens=4096,
            task=TaskType.CHAT,
            reply_reserved=512,
            chat_mode=ChatMode.DEFINE_IDEA,
        )

        system = messages[0]["content"]
        user = messages[-1]["content"]

        self.assertIn("narrative development editor", system.lower())
        self.assertIn("Juan", system)
        self.assertNotIn("Elena", system)
        self.assertIn("forest pass", system)
        self.assertIn("Build unease before the arrival.", system)
        self.assertIn("Third person limited", system)
        self.assertIn("Natural, terse dialogue with subtext.", system)
        self.assertIn("brief dialogue", system.lower())
        self.assertIn("llegue herido al pueblo", user)
        self.assertEqual(len(messages), 2)

    def test_normal_chat_does_not_use_define_idea_instruction(self) -> None:
        project = Project(title="Test Story")
        normal_prompt = build_system_prompt(
            project,
            TaskType.CHAT,
            chat_mode=ChatMode.NORMAL,
        )
        define_prompt = build_system_prompt(
            project,
            TaskType.CHAT,
            chat_mode=ChatMode.DEFINE_IDEA,
        )
        self.assertNotIn("narrative development editor", normal_prompt.lower())
        self.assertIn("narrative development editor", define_prompt.lower())


if __name__ == "__main__":
    unittest.main()
