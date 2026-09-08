import os
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Minimal Qt stubs so the model/context modules can be imported headlessly.
_pyside6 = types.ModuleType("PySide6")
_qtcore = types.ModuleType("PySide6.QtCore")
_qtcore.QObject = object
_qtcore.QThread = object
_qtcore.Signal = lambda *args, **kwargs: None
sys.modules["PySide6"] = _pyside6
sys.modules["PySide6.QtCore"] = _qtcore

from engine.continuity import build_continuity_messages, parse_result
from engine.models import Chapter, Project, Character


class TestContinuityParser(unittest.TestCase):
    def test_valid_pass_with_warning(self):
        result = parse_result(
            '{"passed":true,"issues":[{"category":"timeline","severity":"warning",'
            '"description":"Ambiguous timing.","chapter":"2"}]}'
        )
        self.assertTrue(result["valid"])
        self.assertTrue(result["passed"])
        self.assertEqual(result["issues"][0]["severity"], "warning")

    def test_error_forces_fail_even_when_model_says_passed(self):
        result = parse_result(
            '{"passed":true,"issues":[{"category":"relationship","severity":"error",'
            '"description":"Contradiction.","chapter":"3"}]}'
        )
        self.assertTrue(result["valid"])
        self.assertFalse(result["passed"])

    def test_malformed_result_fails_closed(self):
        result = parse_result("not json")
        self.assertFalse(result["valid"])
        self.assertFalse(result["passed"])

    def test_code_fence_is_accepted(self):
        result = parse_result('```json\n{"passed":true,"issues":[]}\n```')
        self.assertTrue(result["valid"])
        self.assertTrue(result["passed"])


class TestContinuityPrompt(unittest.TestCase):
    def test_prompt_contains_story_state_and_recent_chapters(self):
        project = Project(title="Test Novel")
        project.outline = "## Chapter 1: Arrival\n\nChapter Plan:\nA arrives.\n\nContinuity:\nA is at the station."
        project.world = "# World\n\n## Geography\nThe city is coastal."
        project.memory = "# Current Location\nA is at the station."
        project.characters = [Character(name="A", role="protagonist", description="A young traveler")]
        project.chapters = [Chapter(number=1, title="Arrival", content="A reached the station.")]

        messages = build_continuity_messages(project, 2, "A left the station and returned home.")
        combined = "\n".join(m["content"] for m in messages)

        self.assertIn("A is at the station", combined)
        self.assertIn("The city is coastal", combined)
        self.assertIn("Chapter 1: Arrival", combined)
        self.assertIn("A left the station", combined)


if __name__ == "__main__":
    unittest.main()
