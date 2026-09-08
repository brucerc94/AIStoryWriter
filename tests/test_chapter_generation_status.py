"""
Tests for the chapter generation_status guard introduced in:
  - engine/models.py  (Chapter.generation_status field)
  - engine/workflow.py (_run_write_chapter, _run_write_book)

Scenarios covered
-----------------
1. PASS  → chapter_accepted=True
   • generation_status == "accepted"
   • _extract_and_merge_characters called
   • _update_world_incremental called
   • WRITE_BOOK continues and calls _run_update_memory

2. FAIL (MAX_CONTINUATIONS exceeded with completed=False)
   → chapter_accepted=False
   • generation_status == "incomplete"
   • _extract_and_merge_characters NOT called
   • _update_world_incremental NOT called
   • WRITE_BOOK stops after the incomplete chapter (no next chapter written,
     _run_update_memory not called for the incomplete chapter)

3. Round-trip persistence (backward compat)
   • existing Chapter dict without "generation_status" deserialises to "accepted"
   • Chapter with "generation_status"="incomplete" round-trips correctly
"""

import sys
import os
import unittest
from unittest.mock import MagicMock, patch, call

# ---------------------------------------------------------------------------
# Make the package importable without a full PySide6 install
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub out PySide6 so models/workflow can be imported in a headless environment
import types

_pyside6 = types.ModuleType("PySide6")
_qtcore = types.ModuleType("PySide6.QtCore")


class _FakeSignal:
    """Minimal Qt Signal replacement for tests."""
    def __init__(self, *args):
        self._callbacks = []

    def connect(self, cb):
        self._callbacks.append(cb)

    def emit(self, *args):
        for cb in self._callbacks:
            cb(*args)


class _FakeQObject:
    def __init__(self, *a, **kw):
        pass


class _FakeQThread(_FakeQObject):
    pass


_qtcore.QObject = _FakeQObject
_qtcore.QThread = _FakeQThread
_qtcore.Signal = _FakeSignal
sys.modules["PySide6"] = _pyside6
sys.modules["PySide6.QtCore"] = _qtcore

# Now import the modules under test
from engine.models import Chapter, Project, TaskType          # noqa: E402
from engine.workflow import WorkflowWorker, MAX_CONTINUATIONS  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_worker(outline: str = "") -> WorkflowWorker:
    """Return a WorkflowWorker wired up for write-chapter tests."""
    project = Project(title="Test Novel")
    project.outline = outline
    worker = WorkflowWorker(project=project, task=TaskType.WRITE_CHAPTER)
    # Patch all signals to use our fake
    for attr in ("token_received", "step_started", "step_finished",
                 "error_occurred", "model_loading", "approval_needed",
                 "clear_chat_requested"):
        setattr(worker, attr, _FakeSignal())
    return worker


def _chapter_outline(n: int) -> str:
    return (
        f"## Chapter {n}: Test Chapter {n}\n\n"
        f"Objective:\nObjective for chapter {n}.\n\n"
        "Story Progression:\nSome progression.\n\n"
        "Continuity:\nSome continuity."
    )


# ---------------------------------------------------------------------------
# 1. Chapter passes evaluation → accepted, state updated
# ---------------------------------------------------------------------------

class TestChapterAccepted(unittest.TestCase):

    def _run_passing_write_chapter(self, chapter_num: int = 1):
        worker = _make_worker(outline=_chapter_outline(chapter_num))
        worker.project.current_chapter = chapter_num - 1

        with patch.object(worker, "_load_model_for_task", return_value=True), \
             patch.object(worker, "_model_context_limit", return_value=8192), \
             patch.object(worker, "_run_inference", return_value="Chapter text here."), \
             patch("engine.workflow.change_chapter.plan_chapter", return_value=""), \
             patch("engine.workflow.change_chapter.evaluate_chapter",
                   return_value={"completed": True, "missing": [], "valid": True}), \
             patch.object(worker, "_evaluate_chapter_completion",
                          return_value={"completed": True, "valid": True, "raw": "true"}), \
             patch.object(worker, "_extract_and_merge_characters") as mock_chars, \
             patch.object(worker, "_update_world_incremental") as mock_world, \
             patch("engine.workflow.storage.save_project"):

            worker._run_write_chapter()
            return worker, mock_chars, mock_world

    def test_accepted_chapter_has_status_accepted(self):
        worker, _, _ = self._run_passing_write_chapter(1)
        ch = next(c for c in worker.project.chapters if c.number == 1)
        self.assertEqual(ch.generation_status, "accepted")

    def test_accepted_chapter_updates_characters(self):
        _, mock_chars, _ = self._run_passing_write_chapter(1)
        mock_chars.assert_called_once()

    def test_accepted_chapter_updates_world(self):
        _, _, mock_world = self._run_passing_write_chapter(1)
        mock_world.assert_called_once()


# ---------------------------------------------------------------------------
# 2. Chapter fails (MAX_CONTINUATIONS reached, never passes) → incomplete
# ---------------------------------------------------------------------------

class TestChapterIncomplete(unittest.TestCase):

    def _run_failing_write_chapter(self, chapter_num: int = 1):
        """
        Simulate a chapter that never passes: every evaluation returns
        completed=False until MAX_CONTINUATIONS is exhausted.
        """
        worker = _make_worker(outline=_chapter_outline(chapter_num))
        worker.project.current_chapter = chapter_num - 1

        # Each generation pass returns a short text
        inference_responses = iter(
            ["Initial chapter text."] + [f"Continuation pass {i}." for i in range(2, MAX_CONTINUATIONS + 1)]
        )

        with patch.object(worker, "_load_model_for_task", return_value=True), \
             patch.object(worker, "_model_context_limit", return_value=8192), \
             patch.object(worker, "_run_inference",
                          side_effect=lambda *a, **kw: next(inference_responses, "extra.")), \
             patch("engine.workflow.change_chapter.plan_chapter", return_value=""), \
             patch("engine.workflow.change_chapter.evaluate_chapter",
                   return_value={"completed": False, "missing": ["Not done."], "valid": True}), \
             patch.object(worker, "_evaluate_chapter_completion",
                          return_value={"completed": False, "valid": True, "raw": "false"}), \
             patch("engine.workflow.change_chapter.ends_abruptly", return_value=False), \
             patch("engine.workflow.change_chapter.trim_leading_overlap",
                   side_effect=lambda addition, tail: addition), \
             patch("engine.workflow.change_chapter.is_substantial_duplicate",
                   return_value=False), \
             patch.object(worker, "_extract_and_merge_characters") as mock_chars, \
             patch.object(worker, "_update_world_incremental") as mock_world, \
             patch("engine.workflow.storage.save_project"):

            worker._run_write_chapter()
            return worker, mock_chars, mock_world

    def test_incomplete_chapter_has_status_incomplete(self):
        worker, _, _ = self._run_failing_write_chapter(1)
        ch = next(c for c in worker.project.chapters if c.number == 1)
        self.assertEqual(ch.generation_status, "incomplete")

    def test_incomplete_chapter_content_is_saved(self):
        """Text must be saved even when incomplete."""
        worker, _, _ = self._run_failing_write_chapter(1)
        ch = next(c for c in worker.project.chapters if c.number == 1)
        self.assertTrue(ch.content.strip(), "Content must be saved for incomplete chapters")

    def test_incomplete_chapter_skips_character_update(self):
        _, mock_chars, _ = self._run_failing_write_chapter(1)
        mock_chars.assert_not_called()

    def test_incomplete_chapter_skips_world_update(self):
        _, _, mock_world = self._run_failing_write_chapter(1)
        mock_world.assert_not_called()


# ---------------------------------------------------------------------------
# 3. WRITE_BOOK: incomplete chapter stops the loop, skips memory update
# ---------------------------------------------------------------------------

class TestWriteBookStopsOnIncomplete(unittest.TestCase):

    def _build_worker_with_two_chapter_outline(self) -> WorkflowWorker:
        outline = _chapter_outline(1) + "\n\n" + _chapter_outline(2)
        worker = _make_worker(outline=outline)
        worker.project.current_chapter = 0
        for attr in ("token_received", "step_started", "step_finished",
                     "error_occurred", "model_loading", "approval_needed",
                     "clear_chat_requested"):
            setattr(worker, attr, _FakeSignal())
        return worker

    def test_write_book_stops_after_incomplete_chapter(self):
        """
        When WRITE_CHAPTER produces an incomplete chapter, WRITE_BOOK must
        not proceed to write the next chapter.
        """
        worker = self._build_worker_with_two_chapter_outline()

        chapters_written = []

        def fake_run_write_chapter(self_inner=None):
            # Simulate writing chapter 1 as incomplete
            pending = worker._next_chapter_number()
            ch = Chapter(
                number=pending,
                title=f"Chapter {pending}",
                content="Some text.",
                generation_status="incomplete",
            )
            worker.project.chapters.append(ch)
            chapters_written.append(pending)

        with patch.object(worker, "_run_write_chapter", fake_run_write_chapter), \
             patch.object(worker, "_run_update_memory") as mock_memory, \
             patch.object(worker, "_reload_project_from_storage", return_value=True), \
             patch.object(worker, "_clear_temporary_chat_history"), \
             patch("engine.workflow.storage.save_project"):

            worker._run_write_book()

        self.assertEqual(chapters_written, [1],
                         "Only chapter 1 should have been attempted; chapter 2 must not be written")
        mock_memory.assert_not_called()

    def test_write_book_continues_after_accepted_chapter(self):
        """
        When WRITE_CHAPTER produces an accepted chapter, WRITE_BOOK must
        call _run_update_memory and proceed to the next chapter.
        """
        worker = self._build_worker_with_two_chapter_outline()

        call_counts = {"write": 0}

        def fake_run_write_chapter(self_inner=None):
            pending = worker._next_chapter_number()
            ch = Chapter(
                number=pending,
                title=f"Chapter {pending}",
                content="Good chapter text.",
                generation_status="accepted",
            )
            worker.project.chapters.append(ch)
            call_counts["write"] += 1

        with patch.object(worker, "_run_write_chapter", fake_run_write_chapter), \
             patch.object(worker, "_run_update_memory") as mock_memory, \
             patch.object(worker, "_reload_project_from_storage", return_value=True), \
             patch.object(worker, "_clear_temporary_chat_history"), \
             patch("engine.workflow.storage.save_project"):

            worker._run_write_book()

        self.assertEqual(call_counts["write"], 2,
                         "Both chapters should be written when each is accepted")
        self.assertEqual(mock_memory.call_count, 2,
                         "_run_update_memory must be called once per accepted chapter")


# ---------------------------------------------------------------------------
# 4. Backward-compatibility: Chapter.from_dict without generation_status
# ---------------------------------------------------------------------------

class TestChapterModelBackwardCompat(unittest.TestCase):

    def test_old_chapter_dict_defaults_to_accepted(self):
        d = {"number": 3, "title": "Old Chapter", "content": "content", "id": "abc"}
        ch = Chapter.from_dict(d)
        self.assertEqual(ch.generation_status, "accepted")

    def test_incomplete_chapter_round_trips(self):
        ch = Chapter(number=2, title="Ch 2", content="text", generation_status="incomplete")
        d = ch.to_dict()
        self.assertEqual(d["generation_status"], "incomplete")
        ch2 = Chapter.from_dict(d)
        self.assertEqual(ch2.generation_status, "incomplete")

    def test_accepted_chapter_round_trips(self):
        ch = Chapter(number=1, title="Ch 1", content="text", generation_status="accepted")
        d = ch.to_dict()
        ch2 = Chapter.from_dict(d)
        self.assertEqual(ch2.generation_status, "accepted")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
