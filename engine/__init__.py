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


# The chapter evaluator is intentionally isolated from the large chapter
# workflow module. It returns a single ordered frontier, using two independent
# reads and a third tie-break only when they disagree. This makes the Python
# checklist state authoritative and prevents the evaluator from over-reporting
# arbitrary done IDs.
import importlib.abc
import sys
from importlib.machinery import PathFinder


_TARGET_CHANGE_CHAPTER = "engine.change_chapter"


class _FrontierLoader(importlib.abc.Loader):
    def __init__(self, wrapped):
        self._wrapped = wrapped

    def create_module(self, spec):
        create = getattr(self._wrapped, "create_module", None)
        return create(spec) if create else None

    def exec_module(self, module):
        self._wrapped.exec_module(module)
        from engine.frontier_consensus import install
        install(module)
        from engine.consistency_precheck import install_change_run
        install_change_run(module)


class _FrontierFinder(importlib.abc.MetaPathFinder):
    _ai_story_frontier_finder = True

    def find_spec(self, fullname, path=None, target=None):
        if fullname != _TARGET_CHANGE_CHAPTER:
            return None
        spec = PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return spec
        if isinstance(spec.loader, _FrontierLoader):
            return spec
        spec.loader = _FrontierLoader(spec.loader)
        return spec


if not any(
    getattr(finder, "_ai_story_frontier_finder", False)
    for finder in sys.meta_path
):
    sys.meta_path.insert(0, _FrontierFinder())
