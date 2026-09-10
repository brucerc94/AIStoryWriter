"""UI status hooks for the Generate/Extend Outline workflows."""

from __future__ import annotations

import logging

logger = logging.getLogger("workflow")


def install(module) -> None:
    if getattr(module, "_outline_ui_status_installed", False):
        return

    original_split = getattr(module, "_split_story", None)
    original_write = getattr(module, "_write_chapter_outline", None)
    if original_split is None or original_write is None:
        raise RuntimeError("Outline UI status hook could not find outline helpers.")

    outline_extend_marker = getattr(module, "OUTLINE_EXTEND_MARKER", "__AI_STORY_WRITER_OUTLINE_EXTEND__")

    def split_with_status(worker, requested_count: int, story_source: str):
        is_extend = (worker.extra_input or "").startswith(outline_extend_marker)
        if not is_extend:
            worker.step_started.emit(
                f"Dividing story into {requested_count} chapter source blocks..."
            )
        blocks = original_split(worker, requested_count, story_source)
        if blocks:
            worker.step_started.emit(
                f"Preparing {len(blocks)} chapter contexts..."
            )
        return blocks

    def write_with_status(worker, chapter_num: int, source_block: str, previous_entry: str):
        worker.step_started.emit(
            f"Selecting relevant Characters and World for Chapter {chapter_num}..."
        )
        return original_write(worker, chapter_num, source_block, previous_entry)

    module._split_story = split_with_status
    module._write_chapter_outline = write_with_status
    module._outline_ui_status_installed = True
    logger.info("[generate_outline] UI preparation status hooks installed.")
