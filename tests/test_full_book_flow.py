from __future__ import annotations

import pytest

from engine import export as book_export
from engine import storage
from engine.book_validation import (
    book_content_signature,
    book_export_errors,
    validate_book_structure,
)
from engine.models import Character, Chapter, Project, TaskType
from engine.workflow import WorkflowWorker


def _make_worker(extra_input: str = "") -> WorkflowWorker:
    project = Project(title="Test Book")
    project.synopsis = "A haunted archivist discovers a map that rewrites memory."
    project.outline = "## Chapter 1: The Archive"
    project.world = "## Geography\nA city of libraries and fog."
    project.characters = [Character(name="Mara", role="Protagonist", description="An archivist.")]
    project.auto_book_idea = project.synopsis
    project.auto_book_characters_ready = True
    worker = WorkflowWorker(project=project, task=TaskType.WRITE_BOOK, extra_input=extra_input)
    return worker


def _disable_persistence(monkeypatch):
    monkeypatch.setattr(storage, "save_project", lambda _project: None)


def test_write_book_generates_missing_foundation(monkeypatch):
    worker = _make_worker("Free-form idea for a gothic novel")
    worker.project.synopsis = ""
    worker.project.outline = ""
    worker.project.world = ""
    worker.project.characters = []
    worker.project.auto_book_idea = ""
    worker.project.auto_book_characters_ready = False
    calls: list[str] = []
    _disable_persistence(monkeypatch)

    def fake_write_synopsis():
        calls.append("synopsis")
        worker.project.synopsis = "A haunted archivist discovers a map that rewrites memory."

    def fake_extract_characters(_source):
        calls.append("characters")
        worker.project.characters.append(
            Character(name="Mara", role="Protagonist", description="An archivist.")
        )

    def fake_generate_world():
        calls.append("world")
        worker.project.world = "## Geography\nA city of libraries and fog."

    def fake_generate_outline():
        calls.append("outline")
        worker.project.outline = "## Chapter 1: The Archive"

    def fake_write_chapter():
        calls.append("chapter")
        worker.project.chapters.append(
            Chapter(
                number=1,
                title="The Archive",
                content="The archivist opened the door.",
                generation_status="accepted",
            )
        )

    monkeypatch.setattr(worker, "_run_write_synopsis", fake_write_synopsis)
    monkeypatch.setattr(worker, "_extract_and_merge_characters", fake_extract_characters)
    monkeypatch.setattr(worker, "_run_generate_world", fake_generate_world)
    monkeypatch.setattr(worker, "_run_generate_outline", fake_generate_outline)
    monkeypatch.setattr(worker, "_run_write_chapter", fake_write_chapter)
    monkeypatch.setattr(worker, "_run_update_memory", lambda: True)
    monkeypatch.setattr(worker, "_run_auto_book_literary_review", lambda: True)
    monkeypatch.setattr(worker, "_run_auto_book_final_audit", lambda: True)
    monkeypatch.setattr(worker, "_reload_project_from_storage", lambda: True)
    monkeypatch.setattr(worker, "_clear_temporary_chat_history", lambda: None)

    worker._run_write_book()

    assert calls[:4] == ["synopsis", "characters", "world", "outline"]
    assert calls[-1] == "chapter"
    assert worker.project.chapters[0].generation_status == "accepted"


def test_write_book_updates_memory_after_accepted_chapter(monkeypatch):
    worker = _make_worker()
    worker.project.chapters = []
    memory_calls = []
    _disable_persistence(monkeypatch)

    def fake_write_chapter():
        worker.project.chapters.append(
            Chapter(
                number=1,
                title="The Archive",
                content="The archivist opened the door.",
                generation_status="accepted",
            )
        )

    def fake_update_memory():
        memory_calls.append("memory")
        worker.project.memory = "Mara entered the archive."
        return True

    monkeypatch.setattr(worker, "_run_write_chapter", fake_write_chapter)
    monkeypatch.setattr(worker, "_run_update_memory", fake_update_memory)
    monkeypatch.setattr(worker, "_run_auto_book_literary_review", lambda: True)
    monkeypatch.setattr(worker, "_run_auto_book_final_audit", lambda: True)
    monkeypatch.setattr(worker, "_reload_project_from_storage", lambda: True)
    monkeypatch.setattr(worker, "_clear_temporary_chat_history", lambda: None)

    worker._run_write_book()

    assert memory_calls == ["memory"]


def test_resume_updates_missing_memory_checkpoint_before_next_chapter(monkeypatch):
    worker = _make_worker()
    worker.project.outline = (
        "## Chapter 1: Part One\n\n## Chapter 2: Part Two"
    )
    worker.project.chapters = [
        Chapter(1, "Part One", content="Mara finds the key.", generation_status="accepted")
    ]
    events = []
    _disable_persistence(monkeypatch)

    def fake_write_chapter():
        events.append("write-2")
        worker.project.chapters.append(
            Chapter(2, "Part Two", content="Mara opens the door.", generation_status="accepted")
        )

    def fake_update_memory():
        events.append(f"memory-{worker.project.current_chapter}")
        return True

    monkeypatch.setattr(worker, "_run_write_chapter", fake_write_chapter)
    monkeypatch.setattr(worker, "_run_update_memory", fake_update_memory)
    monkeypatch.setattr(worker, "_run_auto_book_literary_review", lambda: True)
    monkeypatch.setattr(worker, "_run_auto_book_final_audit", lambda: True)
    monkeypatch.setattr(worker, "_reload_project_from_storage", lambda: True)
    monkeypatch.setattr(worker, "_clear_temporary_chat_history", lambda: None)

    worker._run_write_book()

    assert events[:2] == ["memory-1", "write-2"]
    assert worker.project.auto_book_memory_chapter == 2


def test_write_book_stops_after_incomplete_chapter(monkeypatch):
    worker = _make_worker()
    worker.project.chapters = []
    memory_calls = []
    _disable_persistence(monkeypatch)

    def fake_write_chapter():
        worker.project.chapters.append(
            Chapter(
                number=1,
                title="The Archive",
                content="The archivist opened the door.",
                generation_status="incomplete",
            )
        )

    monkeypatch.setattr(worker, "_run_write_chapter", fake_write_chapter)
    monkeypatch.setattr(worker, "_run_update_memory", lambda: memory_calls.append("memory"))
    monkeypatch.setattr(worker, "_reload_project_from_storage", lambda: True)
    monkeypatch.setattr(worker, "_clear_temporary_chat_history", lambda: None)

    worker._run_write_book()

    assert memory_calls == []
    assert worker.project.chapters[0].generation_status == "incomplete"
    assert worker.project.auto_book_status == "paused"


def test_write_book_does_not_review_when_memory_update_fails(monkeypatch):
    worker = _make_worker()
    worker.project.chapters = []
    review_calls = []
    _disable_persistence(monkeypatch)

    def fake_write_chapter():
        worker.project.chapters.append(
            Chapter(
                number=1,
                title="The Archive",
                content="The archivist opened the door.",
                generation_status="accepted",
            )
        )

    monkeypatch.setattr(worker, "_run_write_chapter", fake_write_chapter)
    monkeypatch.setattr(worker, "_run_update_memory", lambda: False)
    monkeypatch.setattr(
        worker,
        "_run_auto_book_literary_review",
        lambda: review_calls.append("review") or True,
    )
    monkeypatch.setattr(worker, "_reload_project_from_storage", lambda: True)
    monkeypatch.setattr(worker, "_clear_temporary_chat_history", lambda: None)

    worker._run_write_book()

    assert worker.project.auto_book_status == "paused"
    assert review_calls == []


def test_resume_selects_first_missing_or_incomplete_outline_chapter():
    worker = _make_worker()
    worker.project.outline = "\n\n".join(
        f"## Chapter {number}: Part {number}" for number in range(1, 4)
    )
    worker.project.chapters = [
        Chapter(1, "Part 1", content="Done.", generation_status="accepted"),
        Chapter(2, "Part 2", content="Partial.", generation_status="incomplete"),
    ]

    assert worker._next_book_chapter_number() == 2


def test_global_review_applies_targeted_correction_and_rechecks(monkeypatch):
    worker = _make_worker()
    chapter = Chapter(1, "The Archive", content="Mara opens the archive.")
    worker.project.chapters = [chapter]
    responses = iter(
        [
            {
                "status": "needs_revision",
                "ending_complete": True,
                "issues": [
                    {
                        "chapter": 1,
                        "severity": "major",
                        "category": "continuity",
                        "instruction": "Clarify why Mara has the brass key.",
                    }
                ],
            },
            {"status": "pass", "ending_complete": True, "issues": []},
        ]
    )
    monkeypatch.setattr(worker, "_request_auto_book_review", lambda: next(responses))
    monkeypatch.setattr(storage, "save_project", lambda _project: None)
    monkeypatch.setattr(worker, "_run_update_memory", lambda: True)

    def fake_change_chapter():
        chapter.content += " She had inherited the key from the former archivist."

    monkeypatch.setattr(worker, "_run_change_chapter", fake_change_chapter)

    assert worker._run_auto_book_literary_review()
    assert "inherited the key" in chapter.content
    assert worker.project.auto_book_review_passed
    assert worker.project.auto_book_review_signature == book_content_signature(worker.project)


def test_final_audit_and_export_gate_detect_post_audit_edits(tmp_path, monkeypatch):
    worker = _make_worker()
    worker.project.chapters = [
        Chapter(1, "The Archive", content="Mara closes the archive.")
    ]
    worker.project.auto_book_review_passed = True
    worker.project.auto_book_review_signature = book_content_signature(worker.project)
    monkeypatch.setattr(storage, "save_project", lambda _project: None)

    assert worker._run_auto_book_final_audit()
    assert worker.project.auto_book_status == "completed"
    assert book_export_errors(worker.project) == []

    worker.project.chapters[0].content += " The key remains unexplained."
    assert any("changed after" in issue for issue in book_export_errors(worker.project))
    for export in (
        book_export.export_to_docx,
        book_export.export_to_pdf,
        book_export.export_to_txt,
        book_export.export_to_markdown,
    ):
        with pytest.raises(RuntimeError, match="cannot be exported"):
            export(worker.project, str(tmp_path / "book.out"))


def test_auto_book_checkpoints_round_trip_through_project_serialization():
    project = Project(
        title="Checkpointed",
        auto_book_status="paused",
        auto_book_stage="writing_chapter_4",
        auto_book_idea="A free-form idea",
        auto_book_characters_ready=True,
        auto_book_review="review output",
        auto_book_review_passed=False,
        auto_book_audit="audit pending",
        auto_book_audit_passed=False,
    )

    loaded = Project.from_dict(project.to_dict())

    assert loaded.auto_book_status == "paused"
    assert loaded.auto_book_stage == "writing_chapter_4"
    assert loaded.auto_book_idea == "A free-form idea"
    assert loaded.auto_book_characters_ready
    assert loaded.auto_book_review == "review output"
    assert loaded.auto_book_audit == "audit pending"


def test_structural_audit_finds_missing_empty_and_out_of_order_chapters():
    project = Project(
        title="Broken",
        outline="## Chapter 1: One\n## Chapter 2: Two\n## Chapter 3: Three",
        chapters=[
            Chapter(1, "One", content="Text."),
            Chapter(3, "Three", content="", generation_status="incomplete"),
        ],
    )

    issues = validate_book_structure(project)

    assert any("Chapter 2 is missing" in issue for issue in issues)
    assert any("Chapter 3 is empty" in issue for issue in issues)
    assert any("Chapter 3 is not accepted (status: incomplete)" in issue for issue in issues)
