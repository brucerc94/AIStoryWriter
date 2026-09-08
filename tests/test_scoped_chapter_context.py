import unittest
from types import SimpleNamespace

from engine.context import build_context_for_model, select_relevant_characters, select_relevant_world
from engine.models import Project, TaskType


class ScopedChapterContextTests(unittest.TestCase):
    def character(self, name):
        return SimpleNamespace(
            name=name, role="supporting", description=f"Description for {name}",
            backstory="", traits=[], relationships=[]
        )

    def test_character_selection(self):
        chars = [self.character("Raul"), self.character("Isabel"), self.character("Manolo")]
        result = select_relevant_characters(chars, "Raul talks to Isabel in the daycare.")
        self.assertIn("Raul", result)
        self.assertIn("Isabel", result)
        self.assertNotIn("Manolo", result)

    def test_world_selection(self):
        world = """# World

## Geography
The baptismal font stands beside the maize fields and the daycare.

## Culture & Customs
Santa Clara holds the Harvest Moon Festival.

## Relevant History
The old church was founded by the first settlers.
"""
        result = select_relevant_world(world, "Raul searches the baptismal font beside the maize fields.", max_sections=2)
        self.assertIn("Geography", result)
        self.assertNotIn("Culture & Customs", result)

    def test_writer_context_has_no_ui_chat_or_memory(self):
        project = Project(title="Test")
        project.characters = [self.character("Raul")]
        project.world = "## Geography\nThe daycare stands beside maize fields."
        project.outline = "## Chapter 1: Test\nRaul enters the daycare."
        project.memory = "LATER CHAPTER EVENT MUST NOT LEAK"
        project.chat_summary = "UI CHAT HISTORY MUST NOT LEAK"
        project.chat_messages = []
        messages = build_context_for_model(
            project,
            "Write Chapter 1. CHAPTER OUTLINE (binding): Raul enters the daycare.",
            "SYSTEM",
            8192,
            task=TaskType.WRITE_CHAPTER,
            reply_reserved=4256,
        )
        self.assertNotIn("LATER CHAPTER EVENT MUST NOT LEAK", messages[0]["content"])
        self.assertNotIn("UI CHAT HISTORY MUST NOT LEAK", messages[0]["content"])


if __name__ == "__main__":
    unittest.main()
