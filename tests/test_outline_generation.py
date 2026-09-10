"""Tests for engine/outline_generation.py

Covers:
  - JSON parsing (_parse_json_object)
  - Chapter block extraction (_extract_chapter_blocks)
  - Author profile construction (_build_author_profile)
  - Treatment completeness detection (_treatment_complete)
  - Continuation prompt rendering (no {{partial_outline}} leak)
  - Chapter user prompt rendering
  - chapter_continue_user.txt merge-conflict resolution
"""

import sys
import unittest
from unittest.mock import MagicMock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.outline_generation import (
    _parse_json_object,
    _extract_chapter_blocks,
    _build_author_profile,
    _treatment_complete,
    _continuation_checkpoint,
    _previous_chapter_context,
    _sanitize_continuation_addition,
    _normalize_outline_entry,
)
from engine import prompts
from engine.models import AuthorIntent, WritingStyle


# ---------------------------------------------------------------------------
# _parse_json_object
# ---------------------------------------------------------------------------

class TestParseJsonObject(unittest.TestCase):

    def test_valid_json(self):
        raw = '{"chapters": [{"number": 1, "source": "Clara arrives"}]}'
        data, reason = _parse_json_object(raw)
        self.assertIsNotNone(data)
        self.assertEqual(reason, "ok")
        self.assertIn("chapters", data)

    def test_json_with_preamble(self):
        """Model output with leading prose before the JSON object."""
        raw = 'Here is the partition:\n{"chapters": [{"number": 1, "source": "Intro"}]}'
        data, reason = _parse_json_object(raw)
        self.assertIsNotNone(data)
        self.assertEqual(reason, "ok")

    def test_trailing_comma_fix(self):
        """Trailing commas are a common local-model mistake — we fix them."""
        raw = '{"chapters": [{"number": 1, "source": "Clara"},]}'
        data, reason = _parse_json_object(raw)
        self.assertIsNotNone(data, f"Should fix trailing comma, got reason: {reason}")
        self.assertEqual(reason, "ok")

    def test_trailing_comma_in_object(self):
        """Trailing comma after last key-value pair in object."""
        raw = '{"chapters": [{"number": 1, "source": "Clara",}]}'
        data, reason = _parse_json_object(raw)
        self.assertIsNotNone(data, f"Should fix trailing comma in object, got: {reason}")

    def test_empty_input(self):
        data, reason = _parse_json_object("")
        self.assertIsNone(data)
        self.assertIn("empty", reason)

    def test_no_json_braces(self):
        raw = "Chapter 1: Clara arrives. Chapter 2: More stuff."
        data, reason = _parse_json_object(raw)
        self.assertIsNone(data)
        self.assertIn("brace", reason)

    def test_invalid_json_unescaped_quote(self):
        """Unescaped double-quote inside a string value — unrecoverable."""
        raw = '{"chapters": [{"number": 1, "source": "has a "quote" inside"}]}'
        data, reason = _parse_json_object(raw)
        self.assertIsNone(data)
        self.assertIn("JSONDecodeError", reason)

    def test_reason_is_never_empty_on_failure(self):
        """Every failure path must provide a non-empty reason string."""
        bad_inputs = ["", "no json", '{"bad": [,]}', "null"]
        for inp in bad_inputs:
            data, reason = _parse_json_object(inp)
            if data is None:
                self.assertTrue(
                    len(reason) > 0,
                    f"Empty reason for input {inp!r}"
                )


# ---------------------------------------------------------------------------
# _extract_chapter_blocks
# ---------------------------------------------------------------------------

class TestExtractChapterBlocks(unittest.TestCase):

    def _make_raw(self, chapters: list[dict]) -> str:
        import json
        return json.dumps({"chapters": chapters})

    def test_valid_two_chapters(self):
        raw = self._make_raw([
            {"number": 1, "source": "Chapter 1 material"},
            {"number": 2, "source": "Chapter 2 material"},
        ])
        blocks = _extract_chapter_blocks(raw, 2)
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0], "Chapter 1 material")
        self.assertEqual(blocks[1], "Chapter 2 material")

    def test_valid_four_chapters(self):
        raw = self._make_raw([
            {"number": i, "source": f"Chapter {i} content"} for i in range(1, 5)
        ])
        blocks = _extract_chapter_blocks(raw, 4)
        self.assertEqual(len(blocks), 4)

    def test_out_of_order_chapters_are_sorted(self):
        raw = self._make_raw([
            {"number": 3, "source": "Third"},
            {"number": 1, "source": "First"},
            {"number": 2, "source": "Second"},
        ])
        blocks = _extract_chapter_blocks(raw, 3)
        self.assertEqual(blocks, ["First", "Second", "Third"])

    def test_missing_chapter_returns_empty(self):
        """Missing chapter 2 should cause failure and return []."""
        raw = self._make_raw([
            {"number": 1, "source": "First"},
            {"number": 3, "source": "Third"},
        ])
        blocks = _extract_chapter_blocks(raw, 3)
        self.assertEqual(blocks, [])

    def test_duplicate_chapter_number_first_wins(self):
        """Second occurrence of the same number should be ignored."""
        raw = self._make_raw([
            {"number": 1, "source": "First"},
            {"number": 1, "source": "Duplicate"},
            {"number": 2, "source": "Second"},
        ])
        blocks = _extract_chapter_blocks(raw, 2)
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0], "First")

    def test_empty_source_skipped(self):
        """Chapter entry with blank source is invalid — missing ch1 → failure."""
        raw = self._make_raw([
            {"number": 1, "source": ""},
            {"number": 2, "source": "Second"},
        ])
        blocks = _extract_chapter_blocks(raw, 2)
        self.assertEqual(blocks, [])

    def test_wrong_count_returns_empty(self):
        raw = self._make_raw([
            {"number": 1, "source": "First"},
        ])
        blocks = _extract_chapter_blocks(raw, 3)
        self.assertEqual(blocks, [])

    def test_chapter_number_zero_skipped(self):
        """Chapter 0 is out of range and should be skipped."""
        raw = self._make_raw([
            {"number": 0, "source": "Zeroth"},
            {"number": 1, "source": "First"},
        ])
        blocks = _extract_chapter_blocks(raw, 1)
        self.assertEqual(blocks, ["First"])

    def test_non_integer_number_skipped(self):
        raw = self._make_raw([
            {"number": "one", "source": "First"},
            {"number": 2, "source": "Second"},
        ])
        # chapter 1 is missing (skipped due to non-int number)
        blocks = _extract_chapter_blocks(raw, 2)
        self.assertEqual(blocks, [])

    def test_trailing_comma_json_end_to_end(self):
        """Trailing comma is fixed at parse level and chapters are extracted."""
        raw = '{"chapters": [{"number": 1, "source": "First"},]}'
        blocks = _extract_chapter_blocks(raw, 1)
        self.assertEqual(blocks, ["First"])

    def test_empty_input_returns_empty(self):
        blocks = _extract_chapter_blocks("", 2)
        self.assertEqual(blocks, [])

    def test_preserves_rich_source_text(self):
        """Source blocks must not be truncated by the extraction logic."""
        long_source = (
            "Clara enters the daycare for the first time. The smell of crayons hits her "
            "immediately. She notices the bright murals on the walls, children's drawings "
            "pinned in uneven rows. Her pulse quickens. Elena emerges from the back office, "
            "extending a hand. She is warm but efficient. She explains the daily routines, "
            "the meal schedule, the nap procedures. Clara nods, trying to absorb it all. "
            "When Elena asks if she has questions, Clara hesitates — she has dozens."
        )
        raw = self._make_raw([{"number": 1, "source": long_source}])
        blocks = _extract_chapter_blocks(raw, 1)
        self.assertEqual(blocks[0], long_source)


# ---------------------------------------------------------------------------
# _build_author_profile
# ---------------------------------------------------------------------------

class TestBuildAuthorProfile(unittest.TestCase):

    def _make_worker(self, intent_kwargs=None, style_kwargs=None):
        worker = MagicMock()
        worker.project.author_intent = AuthorIntent(**(intent_kwargs or {}))
        worker.project.writing_style = WritingStyle(**(style_kwargs or {}))
        return worker

    def test_both_sections_present(self):
        worker = self._make_worker(
            intent_kwargs={"emotional_journey": "Reader feels hope"},
            style_kwargs={"narrator_pov": "Third person limited"},
        )
        profile = _build_author_profile(worker)
        self.assertIn("CREATIVE INTENT:", profile)
        self.assertIn("WRITING STYLE:", profile)
        self.assertIn("hope", profile)
        self.assertIn("Third person limited", profile)

    def test_intent_only_no_style_section(self):
        worker = self._make_worker(
            intent_kwargs={"themes": "Redemption"},
        )
        profile = _build_author_profile(worker)
        self.assertIn("CREATIVE INTENT:", profile)
        self.assertNotIn("WRITING STYLE:", profile)
        self.assertIn("Redemption", profile)

    def test_style_only_no_intent_section(self):
        worker = self._make_worker(
            style_kwargs={"pacing": "Fast"},
        )
        profile = _build_author_profile(worker)
        self.assertNotIn("CREATIVE INTENT:", profile)
        self.assertIn("WRITING STYLE:", profile)
        self.assertIn("Fast", profile)

    def test_empty_profile_returns_fallback(self):
        worker = self._make_worker()
        profile = _build_author_profile(worker)
        self.assertEqual(profile, "(none specified)")

    def test_all_intent_fields_included(self):
        worker = self._make_worker(intent_kwargs={
            "emotional_journey": "Joy to grief",
            "lasting_impression": "Love persists",
            "themes": "Loss, identity",
            "unique_elements": "Magic realism",
            "inspirations": "Garcia Marquez",
            "avoid": "Graphic violence",
        })
        profile = _build_author_profile(worker)
        for fragment in ["Joy to grief", "Love persists", "Loss, identity",
                         "Magic realism", "Garcia Marquez", "Graphic violence"]:
            self.assertIn(fragment, profile, f"Missing: {fragment!r}")

    def test_all_style_fields_included(self):
        worker = self._make_worker(style_kwargs={
            "genre_tags": "Literary fiction",
            "narrator_pov": "First person",
            "pacing": "Slow-burn",
            "description_density": "Rich",
            "dialogue_style": "Sparse",
            "violence_level": "None",
            "romance_level": "Subtle",
            "target_chapter_length": "3000 words",
        })
        profile = _build_author_profile(worker)
        for fragment in ["Literary fiction", "First person", "Slow-burn",
                         "Rich", "Sparse", "None", "Subtle", "3000 words"]:
            self.assertIn(fragment, profile, f"Missing: {fragment!r}")

    def test_sections_separated_by_double_newline(self):
        """The two sections must be clearly separated."""
        worker = self._make_worker(
            intent_kwargs={"themes": "Hope"},
            style_kwargs={"pacing": "Fast"},
        )
        profile = _build_author_profile(worker)
        # There must be a blank line between them
        self.assertIn("\n\n", profile)


# ---------------------------------------------------------------------------
# _treatment_complete
# ---------------------------------------------------------------------------

class TestTreatmentComplete(unittest.TestCase):

    def _make_treatment(self, body: str, chapter_num: int = 1) -> str:
        return f"## Chapter {chapter_num}: The Beginning\n\n{body}"

    def test_complete_treatment(self):
        # Body must be >= 500 chars and have at least 2 paragraphs ending with punctuation
        body = (
            "Clara enters the daycare for the first time. She notices the smell of crayons "
            "and the distant laughter of children drifting from the back room. Her nerves "
            "tighten as she looks around at the bright murals, the cubbyholes, the laminated "
            "schedules pinned to the corkboard. She has imagined this moment for weeks, but "
            "the reality is more overwhelming than she expected.\n\n"
            "Elena appears from behind a door, extending her hand with a warm and efficient smile. "
            "She guides Clara through the space, explaining the daily routines, the meal schedule, "
            "the nap procedures, the emergency protocols. Clara nods, trying to absorb every detail, "
            "aware that her hesitation shows. When Elena asks if she has questions, Clara hesitates "
            "before shaking her head, knowing she has dozens but unsure where to begin."
        )
        self.assertGreaterEqual(len(body), 500, "Test body must be >= 500 chars")
        treatment = self._make_treatment(body)
        self.assertTrue(_treatment_complete(treatment, 1))

    def test_too_short_body_fails(self):
        treatment = self._make_treatment("Short.", 1)
        self.assertFalse(_treatment_complete(treatment, 1))

    def test_missing_heading_fails(self):
        body = "A" * 600 + "\n\nMore content here."
        self.assertFalse(_treatment_complete(body, 1))

    def test_single_paragraph_fails(self):
        """Single unbroken block should fail the paragraph count check."""
        body = "A" * 600  # no blank-line breaks
        treatment = self._make_treatment(body, 1)
        self.assertFalse(_treatment_complete(treatment, 1))

    def test_wrong_chapter_number_fails(self):
        body = "Content.\n\nMore content."
        treatment = f"## Chapter 2: Title\n\n{body}"
        self.assertFalse(_treatment_complete(treatment, 1))

    def test_chapter_three(self):
        # Body must be >= 500 chars and have at least 2 paragraphs
        body = (
            "The confrontation between Clara and the director begins in the narrow corridor "
            "outside the main classroom. Voices rise — controlled at first, then less so. "
            "The director accuses Clara of going over her head by contacting the parents directly "
            "about the incident with the child in the yard. Clara stands her ground, arguing that "
            "protocol demanded it. Neither gives an inch and the air between them grows brittle.\n\n"
            "Elena intervenes, appearing at the corridor entrance and placing herself between them "
            "with a calm, practiced ease. She draws Clara aside with a light hand on her elbow and "
            "leads her into the break room. There she explains, quietly and without drama, what "
            "Clara still does not know: the director has a history, and the parents in question "
            "have leverage. The situation is more complex than it appears from the outside."
        )
        self.assertGreaterEqual(len(body), 500, "Test body must be >= 500 chars")
        treatment = f"## Chapter 3: Tension\n\n{body}"
        self.assertTrue(_treatment_complete(treatment, 3))


# ---------------------------------------------------------------------------
# Continuation prompt — no merge-conflict leak
# ---------------------------------------------------------------------------

class TestContinuationPromptRendering(unittest.TestCase):

    def test_renders_without_error(self):
        """Must render without raising KeyError for any variable."""
        try:
            rendered = prompts.render(
                "outline/chapter_continue_user",
                chapter_number=1,
                source_block="Clara arrives at daycare.",
                author_profile="CREATIVE INTENT:\nHope.\n\nWRITING STYLE:\nThird person.",
                characters="Clara: protagonist",
                world="Urban daycare",
                previous_continuity="(none — this is the first chapter)",
                partial_checkpoint="Clara stepped through the door.",
            )
        except KeyError as e:
            self.fail(f"Render raised KeyError {e!r} — possible unresolved merge conflict")
        self.assertTrue(len(rendered) > 50)

    def test_no_merge_conflict_markers(self):
        raw = prompts.load_raw("outline/chapter_continue_user")
        self.assertNotIn("<<<<<<<", raw, "Git conflict marker found in template")
        self.assertNotIn(">>>>>>>", raw, "Git conflict marker found in template")
        # "=======" is also the git separator — but we need to distinguish from
        # our === SECTION === markers (they have non-whitespace around them).
        conflict_sep = re.compile(r"^={7}\s*$", re.MULTILINE)
        self.assertFalse(
            bool(conflict_sep.search(raw)),
            "Bare '=======' git conflict separator found in template",
        )

    def test_uses_partial_checkpoint_not_partial_outline(self):
        raw = prompts.load_raw("outline/chapter_continue_user")
        self.assertIn("partial_checkpoint", raw)
        self.assertNotIn("partial_outline", raw)

    def test_checkpoint_value_appears_in_rendered_output(self):
        rendered = prompts.render(
            "outline/chapter_continue_user",
            chapter_number=2,
            source_block="The conflict escalates.",
            author_profile="(none specified)",
            characters="(none relevant)",
            world="(none relevant)",
            previous_continuity="Clara finished her first day.",
            partial_checkpoint="Elena left the room abruptly.",
        )
        self.assertIn("Elena left the room abruptly.", rendered)

    def test_author_profile_appears_in_rendered_output(self):
        profile = "CREATIVE INTENT:\nHope and resilience.\n\nWRITING STYLE:\nFirst person intimate."
        rendered = prompts.render(
            "outline/chapter_continue_user",
            chapter_number=1,
            source_block="Scene content.",
            author_profile=profile,
            characters="(none relevant)",
            world="(none relevant)",
            previous_continuity="(none — this is the first chapter)",
            partial_checkpoint="The door opened.",
        )
        self.assertIn("Hope and resilience.", rendered)
        self.assertIn("First person intimate.", rendered)


# ---------------------------------------------------------------------------
# Chapter user prompt rendering
# ---------------------------------------------------------------------------

class TestChapterUserPromptRendering(unittest.TestCase):

    def test_chapter_user_renders_all_fields(self):
        rendered = prompts.render(
            "outline/chapter_user",
            chapter_number=1,
            source_block="Clara arrives at daycare.",
            author_profile="CREATIVE INTENT:\nHope.\n\nWRITING STYLE:\nThird person.",
            characters="Clara: new employee",
            world="Urban daycare",
            previous_continuity="(none — this is the first chapter)",
        )
        self.assertIn("Clara arrives at daycare.", rendered)
        self.assertIn("Hope.", rendered)
        self.assertIn("Clara: new employee", rendered)
        self.assertIn("Urban daycare", rendered)

    def test_chapter_system_renders(self):
        rendered = prompts.render("outline/chapter_system", language_note="")
        self.assertIn("rich", rendered.lower())
        self.assertNotIn("{{", rendered)

    def test_chapter_system_with_language_note(self):
        rendered = prompts.render("outline/chapter_system", language_note=" Write in Spanish.")
        self.assertIn("Write in Spanish.", rendered)

    def test_split_system_renders(self):
        # split_system uses requested_count but the template may not embed the number
        # directly in the system text; what matters is it renders without error
        # and has no unsubstituted placeholders.
        rendered = prompts.render("outline/split_system", requested_count=5)
        self.assertNotIn("{{", rendered)
        self.assertIn("chapter", rendered.lower())

    def test_split_user_renders(self):
        rendered = prompts.render(
            "outline/split_user",
            requested_count=3,
            story_source="Clara works at a daycare.",
        )
        self.assertIn("Clara works", rendered)
        self.assertIn("3", rendered)


# ---------------------------------------------------------------------------
# _continuation_checkpoint
# ---------------------------------------------------------------------------

class TestContinuationCheckpoint(unittest.TestCase):

    def test_short_text_returned_as_is(self):
        text = "Short partial treatment."
        result = _continuation_checkpoint(text)
        self.assertEqual(result, text)

    def test_long_text_truncated_from_end(self):
        text = "A" * 5000
        result = _continuation_checkpoint(text)
        self.assertLessEqual(len(result), 2450)
        self.assertTrue(result.endswith("A"))

    def test_empty_returns_empty(self):
        self.assertEqual(_continuation_checkpoint(""), "")

    def test_tail_content_preserved(self):
        """The END of the text must be preserved (not the start).
        
        We use a clearly unique start sentinel that won't appear in the 2400-char tail.
        The word 'BEGINNING' is 10 chars, so 300 repetitions = 3000 chars.
        The checkpoint keeps the last 2400 chars, which still contains 'BEGINNING'.
        Instead we verify that a unique start marker is NOT in the result.
        """
        unique_start = "UNIQUE_START_SENTINEL_XYZ"
        text = unique_start + "A" * 3000 + "END_MARKER"
        result = _continuation_checkpoint(text)
        self.assertIn("END_MARKER", result)
        self.assertNotIn(unique_start, result)


# ---------------------------------------------------------------------------
# _previous_chapter_context
# ---------------------------------------------------------------------------

class TestPreviousChapterContext(unittest.TestCase):

    def test_empty_returns_none_message(self):
        result = _previous_chapter_context("")
        self.assertIn("none", result.lower())

    def test_long_entry_truncated(self):
        entry = "## Chapter 1: Title\n\n" + "B" * 5000
        result = _previous_chapter_context(entry)
        self.assertLessEqual(len(result), 2450)

    def test_short_entry_preserved(self):
        entry = "## Chapter 1: Title\n\nClara finished her first day."
        result = _previous_chapter_context(entry)
        self.assertIn("Clara finished", result)


# ---------------------------------------------------------------------------
# _sanitize_continuation_addition
# ---------------------------------------------------------------------------

class TestSanitizeContinuationAddition(unittest.TestCase):

    def test_strips_chapter_heading(self):
        text = "## Chapter 3: Continued\n\nShe walked into the room."
        result = _sanitize_continuation_addition(text, 3)
        self.assertNotIn("## Chapter 3", result)
        self.assertIn("She walked into the room.", result)

    def test_strips_code_fence(self):
        text = "```\nShe walked.\n```"
        result = _sanitize_continuation_addition(text, 1)
        self.assertIn("She walked.", result)
        self.assertNotIn("```", result)

    def test_empty_returns_empty(self):
        self.assertEqual(_sanitize_continuation_addition("", 1), "")

    def test_strips_chapter_plan_label(self):
        text = "Chapter Plan:\nShe walked.\n\nShe spoke."
        result = _sanitize_continuation_addition(text, 1)
        self.assertNotIn("Chapter Plan:", result)

    def test_preserves_narrative_content(self):
        """Narrative body must survive sanitisation intact."""
        body = "She stepped forward.\n\nThe room fell silent."
        text = f"## Chapter 2: Scene\n\n{body}"
        result = _sanitize_continuation_addition(text, 2)
        self.assertIn("She stepped forward.", result)
        self.assertIn("The room fell silent.", result)


# ---------------------------------------------------------------------------
# _normalize_outline_entry
# ---------------------------------------------------------------------------

class TestNormalizeOutlineEntry(unittest.TestCase):

    def test_strips_leading_text_before_heading(self):
        text = "Some preamble.\n\n## Chapter 1: Title\n\nContent here."
        result = _normalize_outline_entry(text, 1)
        self.assertTrue(result.startswith("## Chapter 1"))

    def test_strips_code_fence(self):
        text = "```\n## Chapter 1: Title\n\nContent.\n```"
        result = _normalize_outline_entry(text, 1)
        self.assertNotIn("```", result)

    def test_returns_empty_for_empty_input(self):
        self.assertEqual(_normalize_outline_entry("", 1), "")

    def test_spanish_heading_recognized(self):
        text = "## Capítulo 2: El encuentro\n\nContenido."
        result = _normalize_outline_entry(text, 2)
        self.assertIn("Capítulo 2", result)


import re  # needed for the conflict marker regex test


if __name__ == "__main__":
    unittest.main(verbosity=2)
