import hashlib
import unittest

from engine.models import Project


class OutlineSourceBoundaryTests(unittest.TestCase):
    def test_outline_source_boundary_round_trips(self) -> None:
        synopsis = (
            "Raul reaches the town and discovers the hidden gate. "
            "He decides to stay until he understands what happened."
        )
        project = Project(
            title="Test Story",
            synopsis=synopsis,
            outline="## Chapter 1: Arrival\nRaul reaches the town.",
        )

        project.outline_source_length = len(synopsis)
        project.outline_source_hash = hashlib.sha256(
            synopsis.encode("utf-8")
        ).hexdigest()

        restored = Project.from_dict(project.to_dict())

        self.assertEqual(restored.outline_source_length, len(synopsis))
        self.assertEqual(restored.outline_source_hash, project.outline_source_hash)

    def test_new_synopsis_material_starts_at_saved_boundary(self) -> None:
        processed = "Raul reaches the town."
        added = " He discovers a hidden gate."
        synopsis = processed + added

        boundary = len(processed)
        source_hash = hashlib.sha256(
            synopsis[:boundary].encode("utf-8")
        ).hexdigest()

        self.assertEqual(synopsis[:boundary], processed)
        self.assertEqual(synopsis[boundary:], added)
        self.assertEqual(source_hash, hashlib.sha256(processed.encode("utf-8")).hexdigest())


if __name__ == "__main__":
    unittest.main()
