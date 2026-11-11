"""Engine package initialization."""

# Continuation prompts pass a slot named ``prose_tail`` through the shared
# budget allocator. The allocator truncates text from the left by default,
# which is correct for most context sections but wrong for a tail: a tail must
# always end at the actual end of the chapter. Wrap the shared allocator at
# package load time so both Write Chapter and Change Chapter preserve that
# invariant without duplicating allocation logic in either workflow.
from engine import context as _context

_original_budget_allocate = _context.budget_allocate


def _budget_allocate_preserving_tails(total_chars, slots):
    allocated = _original_budget_allocate(total_chars, slots)
    for name, full_text, _min_chars in slots:
        if name != "prose_tail":
            continue
        text = (full_text or "").strip()
        if not text:
            allocated[name] = ""
            continue
        current = allocated.get(name, "") or ""
        if current:
            allocated[name] = text[-len(current):]
        else:
            allocated[name] = ""
    return allocated


_context.budget_allocate = _budget_allocate_preserving_tails


# Chapter evaluators and authoring-stage hooks are isolated from the large
# workflow modules.
import importlib.abc
import re
import sys
from importlib.machinery import PathFinder


_TARGETS = {"engine.change_chapter", "engine.workflow"}


class _EngineModuleLoader(importlib.abc.Loader):
    def __init__(self, wrapped, fullname):
        self._wrapped = wrapped
        self._fullname = fullname

    def create_module(self, spec):
        create = getattr(self._wrapped, "create_module", None)
        return create(spec) if create else None

    def exec_module(self, module):
        self._wrapped.exec_module(module)
        if self._fullname == "engine.change_chapter":
            from engine.frontier_consensus import install as install_frontier
            install_frontier(module)
            from engine.consistency_precheck import install_change_run
            install_change_run(module)
        elif self._fullname == "engine.workflow":
            from engine.outline_generation import install as install_outline
            install_outline(module)
            from engine.synopsis_draft import install as install_synopsis_draft
            install_synopsis_draft(module)

            worker_cls = getattr(module, "WorkflowWorker", None)
            outline_runner = getattr(worker_cls, "_run_generate_outline", None) if worker_cls else None
            if worker_cls and outline_runner and not getattr(worker_cls, "_outline_start_status_installed", False):
                def _run_generate_outline_with_start_status(worker, _runner=outline_runner):
                    extra = worker.extra_input or ""
                    if not extra.startswith("__AI_STORY_WRITER_OUTLINE_EXTEND__"):
                        worker.step_started.emit("Starting outline generation — dividing story into chapter blocks...")
                    return _runner(worker)

                worker_cls._run_generate_outline = _run_generate_outline_with_start_status
                worker_cls._outline_start_status_installed = True

            # Keep the existing sequential Write Chapter pipeline, adding
            # durable checkpoints and the final review/audit stages.
            original_write_book = getattr(worker_cls, "_run_write_book", None) if worker_cls else None
            if worker_cls and original_write_book and not getattr(worker_cls, "_write_book_no_memory_installed", False):
                def _run_write_book_with_completion_pipeline(worker):
                    logger = getattr(module, "logger", None)
                    if not worker._ensure_auto_book_foundation():
                        worker.project.auto_book_status = "paused"
                        worker._checkpoint_auto_book()
                        worker.step_finished.emit(
                            "Full Book Progress",
                            worker.project.auto_book_audit or worker.project.auto_book_stage,
                        )
                        return
                    logger.info("[write_book] Clearing chat before Chapter 1.") if logger else None
                    worker._clear_temporary_chat_history()
                    worker.clear_chat_requested.emit()
                    worker.step_started.emit("Clearing chat...")

                    outline_numbers = worker._outline_chapter_numbers()
                    total = len(outline_numbers) if outline_numbers else max(1, len(worker.project.chapters) + 1)
                    written = 0

                    worker.step_started.emit(f"Writing Full Book (0/{total})...")

                    while not worker._cancelled:
                        if not worker._reload_project_from_storage():
                            break

                        pending_memory = worker._next_book_memory_chapter_number()
                        if pending_memory:
                            worker.project.current_chapter = pending_memory
                            worker._checkpoint_auto_book(stage=f"memory_chapter_{pending_memory}")
                            worker.step_started.emit(
                                f"Updating Story Memory for Chapter {pending_memory}..."
                            )
                            if not worker._run_update_memory():
                                worker.project.auto_book_status = "paused"
                                worker._checkpoint_auto_book()
                                worker.error_occurred.emit(
                                    f"Story Memory was not updated for Chapter {pending_memory}; "
                                    "generation paused before the next chapter."
                                )
                                break
                            worker.project.auto_book_memory_chapter = max(
                                worker.project.auto_book_memory_chapter,
                                pending_memory,
                            )
                            worker._checkpoint_auto_book(stage="writing_chapters")
                            continue

                        current_outline_numbers = worker._outline_chapter_numbers()
                        pending = worker._next_book_chapter_number()
                        if pending <= 0:
                            break

                        if worker.project.outline and not worker._outline_has_chapter(pending):
                            if logger:
                                logger.info(
                                    f"[write_book] Chapter {pending} has no outline entry "
                                    f"(outline covers: {current_outline_numbers}). Stopping."
                                )
                            break

                        worker.project.current_chapter = pending - 1
                        worker._checkpoint_auto_book(stage=f"writing_chapter_{pending}")
                        worker.step_started.emit(f"Writing Chapter {pending}/{total}...")
                        if logger:
                            logger.info(f"[write_book] Writing Chapter {pending}/{total} via Write Chapter.")
                        worker._run_write_chapter()
                        written += 1

                        if worker._cancelled:
                            break

                        chapter = next(
                            (c for c in worker.project.chapters if c.number == pending),
                            None,
                        )
                        if chapter is None:
                            if logger:
                                logger.warning(
                                    f"[write_book] Chapter {pending} was not produced; stopping."
                                )
                            worker.project.auto_book_status = "paused"
                            worker._checkpoint_auto_book()
                            break

                        if chapter.generation_status != "accepted":
                            worker.step_started.emit(
                                f"Chapter {pending} is not accepted by completion checks. "
                                "Stopping book generation — please review or regenerate it."
                            )
                            if logger:
                                logger.warning(
                                    f"[write_book] Chapter {pending} is not accepted; stopping full-book generation."
                                )
                            worker.project.auto_book_status = "paused"
                            worker._checkpoint_auto_book()
                            break

                        worker.project.current_chapter = pending
                        logger.info(f"[write_book] Updating Story Memory for Chapter {pending}.") if logger else None
                        worker.step_started.emit(f"Updating Story Memory for Chapter {pending}...")
                        if not worker._run_update_memory():
                            worker.project.auto_book_status = "paused"
                            worker._checkpoint_auto_book(stage=f"memory_chapter_{pending}")
                            worker.error_occurred.emit(
                                f"Story Memory was not updated for Chapter {pending}; "
                                "generation paused before the next chapter."
                            )
                            break
                        worker.project.auto_book_memory_chapter = max(
                            worker.project.auto_book_memory_chapter,
                            pending,
                        )
                        worker._checkpoint_auto_book(stage="writing_chapters")

                        if worker._stop_after_current_chapter:
                            if logger:
                                logger.info("[write_book] Stop requested after current chapter.")
                            worker.project.auto_book_status = "paused"
                            worker._checkpoint_auto_book()
                            break

                        if not worker._reload_project_from_storage():
                            break

                        next_pending = worker._next_chapter_number()
                        if next_pending <= pending:
                            break
                        if worker.project.outline and not worker._outline_has_chapter(next_pending):
                            if logger:
                                logger.info(
                                    f"[write_book] Next chapter {next_pending} has no outline entry. Stopping."
                                )
                            break

                        logger.info(f"[write_book] Clearing chat before Chapter {pending}.") if logger else None
                        worker._clear_temporary_chat_history()
                        worker.clear_chat_requested.emit()
                        worker.step_started.emit("Clearing chat...")

                    if logger:
                        if written == 0:
                            logger.info("[write_book] No pending outline chapters found.")
                        else:
                            logger.info(f"[write_book] Finished writing {written} chapter(s).")

                    if worker._cancelled or worker._stop_after_current_chapter:
                        worker.project.auto_book_status = "paused"
                        worker._checkpoint_auto_book()
                        return
                    if worker.project.auto_book_status == "paused":
                        worker.step_finished.emit(
                            "Full Book Progress",
                            worker.project.auto_book_audit or worker.project.auto_book_stage,
                        )
                        return

                    from engine.book_validation import validate_book_structure
                    structural_issues = validate_book_structure(worker.project)
                    if structural_issues:
                        worker.project.auto_book_status = "paused"
                        worker.project.auto_book_audit_passed = False
                        worker.project.auto_book_audit = (
                            "Pre-review validation failed:\n- " + "\n- ".join(structural_issues)
                        )
                        worker._checkpoint_auto_book(stage="final_audit")
                        worker.step_finished.emit("Full Book Progress", worker.project.auto_book_audit)
                        worker.error_occurred.emit(
                            "The manuscript is not ready for global review:\n- "
                            + "\n- ".join(structural_issues)
                        )
                        return

                    if not worker._run_auto_book_literary_review():
                        worker.step_finished.emit(
                            "Full Book Progress",
                            worker.project.auto_book_review or worker.project.auto_book_audit,
                        )
                        return
                    worker._run_auto_book_final_audit()
                    worker.step_finished.emit("Full Book Progress", worker.project.auto_book_audit)

                worker_cls._run_write_book = _run_write_book_with_completion_pipeline
                worker_cls._write_book_no_memory_installed = True

            original_parse_json_object = getattr(module, "_parse_json_object", None)
            if original_parse_json_object is not None and not getattr(module, "_loose_json_parser_installed", False):
                def _parse_json_object_with_recovery(raw, _original=original_parse_json_object):
                    parsed, reason = _original(raw)
                    if parsed is not None:
                        return parsed, reason

                    text = (raw or "").strip()
                    pattern = re.compile(
                        r'\{\s*"number"\s*:\s*(\d+)\s*,\s*"source"\s*:\s*"',
                        re.DOTALL,
                    )
                    matches = list(pattern.finditer(text))
                    if not matches:
                        return parsed, reason

                    chapters = []
                    for index, match in enumerate(matches):
                        start = match.end()
                        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
                        segment = text[start:end].rstrip()
                        if not segment:
                            continue

                        quote_index = segment.rfind('"')
                        if quote_index >= 0:
                            tail = segment[quote_index + 1 :].strip()
                            if not tail or re.fullmatch(r'[\s,\]\}]*', tail):
                                source = segment[:quote_index].rstrip()
                            else:
                                source = segment
                        else:
                            source = segment

                        source = source.rstrip().rstrip(',').rstrip()
                        if source.endswith('}'):
                            source = source[:-1].rstrip()
                        if source.endswith(']'):
                            source = source[:-1].rstrip()
                        if source.endswith('"'):
                            source = source[:-1]

                        if source.strip():
                            chapters.append({"number": int(match.group(1)), "source": source.strip()})

                    if chapters:
                        logger = getattr(module, "logger", None)
                        if logger is not None:
                            logger.warning(
                                "[generate_outline] Recovered %d chapter object(s) from malformed Stage 1 JSON.",
                                len(chapters),
                            )
                        return {"chapters": chapters}, "recovered malformed chapter JSON"
                    return parsed, reason

                module._parse_json_object = _parse_json_object_with_recovery
                module._loose_json_parser_installed = True


class _EngineModuleFinder(importlib.abc.MetaPathFinder):
    _ai_story_engine_finder = True

    def find_spec(self, fullname, path=None, target=None):
        if fullname not in _TARGETS:
            return None
        spec = PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return spec
        if isinstance(spec.loader, _EngineModuleLoader):
            return spec
        spec.loader = _EngineModuleLoader(spec.loader, fullname)
        return spec


if not any(
    getattr(finder, "_ai_story_engine_finder", False)
    for finder in sys.meta_path
):
    sys.meta_path.insert(0, _EngineModuleFinder())
