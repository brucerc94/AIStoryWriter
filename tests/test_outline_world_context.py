import unittest

from engine.context import build_context_for_model, build_system_prompt
from engine.models import Project, TaskType


class OutlineWorldContextTests(unittest.TestCase):
    def test_generate_outline_includes_world_reference(self):
        project = Project(
            title="Test",
            synopsis="Synopsis",
            world="## Geography\nThe village has an established river boundary.",
        )
        system_prompt = build_system_prompt(project, TaskType.GENERATE_OUTLINE)
        messages = build_context_for_model(
            project,
            "Generate the outline.\n\nSynopsis: Synopsis",
            system_prompt,
            max_context_tokens=4096,
            task=TaskType.GENERATE_OUTLINE,
            reply_reserved=512,
        )
        self.assertIn("The village has an established river boundary.", messages[0]["content"])
        self.assertIn("## World & Setting", messages[0]["content"])


if __name__ == "__main__":
    unittest.main()
