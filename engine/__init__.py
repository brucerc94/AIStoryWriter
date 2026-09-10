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

            # Show immediate feedback while Generate Outline is performing
            # its initial semantic split. The outline module itself reports
            # the chapter-level states afterward.
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
