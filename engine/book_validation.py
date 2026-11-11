"""Final-book validation shared by workflow completion and all export formats."""

from __future__ import annotations

import hashlib
import json
import re

from engine.models import Project


_OUTLINE_CHAPTER_RE = re.compile(
    r"(?im)^\s*##\s*Chapter\s+(\d+)\b"
)


def book_content_signature(project: Project) -> str:
    """Fingerprint the audited canon and manuscript so edits invalidate completion."""
    payload = {
        "title": project.title,
        "synopsis": project.synopsis,
        "outline": project.outline,
        "world": project.world,
        "memory": project.memory,
        "characters": [character.to_dict() for character in project.characters],
        "chapters": [
            {
                "number": chapter.number,
                "title": chapter.title,
                "content": chapter.content,
                "generation_status": chapter.generation_status,
            }
            for chapter in sorted(project.chapters, key=lambda item: item.number)
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_book_structure(project: Project) -> list[str]:
    """Return structural problems that make a manuscript unsafe to mark complete."""
    outline_numbers = sorted(
        {int(match.group(1)) for match in _OUTLINE_CHAPTER_RE.finditer(project.outline or "")}
    )
    if not outline_numbers:
        return ["The outline contains no detectable chapters."]

    expected_numbers = list(range(1, len(outline_numbers) + 1))
    issues: list[str] = []
    if outline_numbers != expected_numbers:
        issues.append(
            "Outline chapters must be numbered consecutively from 1; "
            f"found {outline_numbers}."
        )

    chapters_by_number: dict[int, list] = {}
    for chapter in project.chapters:
        chapters_by_number.setdefault(chapter.number, []).append(chapter)

    for number in expected_numbers:
        matches = chapters_by_number.get(number, [])
        if not matches:
            issues.append(f"Chapter {number} is missing.")
            continue
        if len(matches) > 1:
            issues.append(f"Chapter {number} appears more than once.")
            continue
        chapter = matches[0]
        if not chapter.content.strip():
            issues.append(f"Chapter {number} is empty.")
        if chapter.generation_status != "accepted":
            issues.append(
                f"Chapter {number} is not accepted (status: {chapter.generation_status})."
            )

    unexpected_numbers = sorted(set(chapters_by_number) - set(expected_numbers))
    if unexpected_numbers:
        issues.append(f"Unexpected chapter number(s): {unexpected_numbers}.")
    return issues


def book_export_errors(project: Project) -> list[str]:
    """Return reasons the project cannot be exported as a finished book."""
    issues = validate_book_structure(project)
    if not project.auto_book_review_passed:
        issues.append("The global literary review has not passed.")
    if not project.auto_book_audit_passed:
        issues.append("The final audit has not passed.")
    if project.auto_book_status != "completed":
        issues.append("Full-book creation is not marked complete.")
    if (
        not project.auto_book_audit_signature
        or project.auto_book_audit_signature != book_content_signature(project)
    ):
        issues.append("The manuscript or story canon changed after the final audit.")
    if (
        not project.auto_book_review_signature
        or project.auto_book_review_signature != book_content_signature(project)
    ):
        issues.append("The manuscript or story canon changed after the global review.")
    return issues
