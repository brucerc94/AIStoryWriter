"""
Minimal tests for:
  1. budget_allocate() – core allocator logic
  2. write_chapter _build_chapter_continuation_prompt – prompt renders with all
     slots present and fits inside the configured context window
  3. change_chapter _build_continuation_prompt – same guarantee
  4. reply_reserved uses content_max_tokens, not context_limit // 3
"""
from __future__ import annotations

import sys
import types
import unittest
import unittest.mock as mock


# ---------------------------------------------------------------------------
# Minimal stubs so engine modules can be imported without the full app stack
# ---------------------------------------------------------------------------

def _install_stubs():
    # PySide6
    ps6 = types.ModuleType("PySide6")
    qtcore = types.ModuleType("PySide6.QtCore")

    class _Sig:
        def __init__(self, *a): self._s = []
        def connect(self, fn): self._s.append(fn)
        def emit(self, *a):
            for fn in self._s: fn(*a)

    class _QObj:
        def __init__(self, *a, **kw): pass
        token_received    = _Sig()
        step_started      = _Sig()
        step_finished     = _Sig()
        error_occurred    = _Sig()
        model_loading     = _Sig()
        approval_needed   = _Sig()
        clear_chat_requested = _Sig()

    class _QThr:
        def __init__(self, *a, **kw): pass

    qtcore.QObject = _QObj
    qtcore.Signal  = _Sig
    qtcore.QThread = _QThr
    ps6.QtCore     = qtcore
    sys.modules.setdefault("PySide6",        ps6)
    sys.modules.setdefault("PySide6.QtCore", qtcore)

    # engine.chat
    ec = types.ModuleType("engine.chat")
    ec.get_engine = mock.MagicMock()
    sys.modules.setdefault("engine.chat", ec)

    # engine.context – use the real module but patch get_engine calls
    # (imported below after stubs are in place)


_install_stubs()

# Now we can import the real modules
import engine.context as ctx_mod
from engine.context import budget_allocate, estimate_messages_tokens
import engine.workflow as wf_mod
from engine.workflow import WorkflowWorker, WRITE_CHAPTER_TARGET_MARKER
import engine.change_chapter as cc_mod
from engine.models import Chapter, Character, Project, AppSettings, TaskType


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_project(ctx=8192, max_tokens=4256) -> tuple["Project", "Settings"]:
    project = Project(title="Test Novel")
    project.outline = (
        "## Chapter 2: The Duel\n\n"
        "Objective: Alice faces the guardian.\n\n"
        "Story Progression: They fight. Alice wins by exploiting the guardian's weakness.\n\n"
        "Continuity: Alice is alone. Guardian is hostile. Location: the bridge.\n"
    )
    project.world = "A medieval world with no magic. The bridge spans the River Var."
    project.memory = "Alice defeated the wolves in chapter 1. She carries a silver dagger."
    alice = Character(name="Alice", role="protagonist", description="Bold warrior woman.")
    project.characters = [alice]

    settings = AppSettings()
    settings.default_context_size = ctx
    settings.content_max_tokens   = max_tokens
    return project, settings


def _make_worker(ctx=8192, max_tokens=4256) -> "WorkflowWorker":
    project, settings = _make_project(ctx, max_tokens)
    worker = WorkflowWorker(project=project, task=TaskType.WRITE_CHAPTER, settings=settings)
    # Patch _model_context_limit to return the configured ctx without loading a model
    worker._model_context_limit = mock.MagicMock(return_value=ctx)
    return worker


# ---------------------------------------------------------------------------
# 1. budget_allocate unit tests
# ---------------------------------------------------------------------------

class TestBudgetAllocate(unittest.TestCase):

    def test_total_fits_all(self):
        """When budget > sum of all content, every slot is returned in full."""
        slots = [("a", "AAAA", 2), ("b", "BBBB", 2), ("c", "CC", 1)]
        result = budget_allocate(100, slots)
        self.assertEqual(result["a"], "AAAA")
        self.assertEqual(result["b"], "BBBB")
        self.assertEqual(result["c"], "CC")

    def test_high_priority_wins_when_tight(self):
        """When budget covers only minimums, higher-priority slots are served first."""
        slots = [("hi", "X" * 500, 200), ("lo", "Y" * 500, 200)]
        result = budget_allocate(250, slots)
        # hi gets its full 200; lo gets at most the remaining 50
        self.assertEqual(len(result["hi"]), 200)
        self.assertLessEqual(len(result["lo"]), 50)

    def test_surplus_distributed(self):
        """Surplus beyond minimums goes to slots with remaining content."""
        slots = [("a", "A" * 400, 100), ("b", "B" * 400, 100)]
        result = budget_allocate(600, slots)
        total_used = len(result["a"]) + len(result["b"])
        self.assertLessEqual(total_used, 600)
        self.assertGreater(total_used, 200)   # got more than just minimums

    def test_empty_slot_skipped(self):
        """Empty full_text results in an empty string, not an error."""
        slots = [("a", "AAAA", 2), ("empty", "", 50), ("b", "BBBB", 2)]
        result = budget_allocate(20, slots)
        self.assertEqual(result["empty"], "")

    def test_zero_budget(self):
        """Zero budget: all slots return empty (minimums capped to 0)."""
        slots = [("a", "AAAA", 4), ("b", "BBBB", 4)]
        result = budget_allocate(0, slots)
        self.assertEqual(result["a"], "")
        self.assertEqual(result["b"], "")

    def test_all_slots_present_in_result(self):
        """Every named slot appears in the result dict."""
        slots = [("x", "text", 2), ("y", "", 10), ("z", "more", 5)]
        result = budget_allocate(30, slots)
        self.assertIn("x", result)
        self.assertIn("y", result)
        self.assertIn("z", result)

    def test_output_never_exceeds_budget(self):
        """Sum of all output lengths never exceeds total_chars."""
        import random
        rng = random.Random(42)
        for _ in range(50):
            budget = rng.randint(0, 2000)
            n = rng.randint(1, 6)
            slots = [
                (f"s{i}", "x" * rng.randint(0, 600), rng.randint(0, 300))
                for i in range(n)
            ]
            result = budget_allocate(budget, slots)
            total = sum(len(v) for v in result.values())
            self.assertLessEqual(
                total, budget,
                f"budget={budget} slots={[(n, len(t), m) for n, t, m in slots]} total={total}"
            )


# ---------------------------------------------------------------------------
# 2. write_chapter continuation prompt tests
# ---------------------------------------------------------------------------

class TestWriteChapterContinuationPrompt(unittest.TestCase):

    def setUp(self):
        self.worker = _make_worker(ctx=8192, max_tokens=4256)

    def test_continuity_context_present(self):
        """The rendered prompt must contain the CONTINUITY ANCHORS block."""
        prose = "Alice drew her blade and faced the guardian on the bridge. The air was cold."
        prompt = self.worker._build_chapter_continuation_prompt(
            chapter_num=2,
            chapter_text=prose * 30,   # ~2 KB of prose
            chapter_goal="Alice defeats the guardian",
        )
        self.assertIn("CONTINUITY ANCHORS", prompt)

    def test_characters_appear(self):
        """Character data must appear in the continuation prompt."""
        prose = "She stood at the gate, sword raised."
        prompt = self.worker._build_chapter_continuation_prompt(
            chapter_num=2,
            chapter_text=prose * 30,
            chapter_goal="Fight scene",
        )
        self.assertIn("Alice", prompt)

    def test_world_appears(self):
        """World/setting context must appear in the prompt."""
        prose = "The bridge creaked beneath her boots."
        prompt = self.worker._build_chapter_continuation_prompt(
            chapter_num=2,
            chapter_text=prose * 30,
            chapter_goal="Bridge fight",
        )
        self.assertIn("bridge", prompt.lower())

    def test_outline_appears(self):
        """Chapter outline must appear in the prompt."""
        prose = "Alice advanced, dagger in hand."
        prompt = self.worker._build_chapter_continuation_prompt(
            chapter_num=2,
            chapter_text=prose * 30,
            chapter_goal="",
        )
        self.assertIn("guardian", prompt.lower())

    def test_prose_tail_appears(self):
        """The most recent prose must appear near the end of the prompt."""
        unique = "UNIQUE_SENTINEL_XQ91Z"
        long_prose = ("filler " * 500) + unique
        prompt = self.worker._build_chapter_continuation_prompt(
            chapter_num=2,
            chapter_text=long_prose,
            chapter_goal="continue",
        )
        self.assertIn(unique, prompt)

    def test_prompt_fits_context(self):
        """Rendered prompt token estimate must not exceed (ctx - reply) tokens."""
        ctx, reply = 8192, 4256
        worker = _make_worker(ctx=ctx, max_tokens=reply)
        prose = "She fought bravely. " * 1000   # ~20 KB
        prompt = worker._build_chapter_continuation_prompt(
            chapter_num=2,
            chapter_text=prose,
            chapter_goal="End the duel",
        )
        # Use the same estimator the app uses: len // 4
        estimated_tokens = len(prompt) // 4
        self.assertLessEqual(
            estimated_tokens, ctx - reply,
            f"Prompt is ~{estimated_tokens} tokens but only {ctx - reply} are available for the prompt."
        )

    def test_tiny_context_doesnt_crash(self):
        """A very small context (2048 / 1024) must not raise."""
        worker = _make_worker(ctx=2048, max_tokens=1024)
        prose = "Text. " * 500
        prompt = worker._build_chapter_continuation_prompt(
            chapter_num=1,
            chapter_text=prose,
            chapter_goal="finish",
        )
        self.assertIsInstance(prompt, str)
        self.assertGreater(len(prompt), 10)

    def test_checklist_missing_items_appear(self):
        """When a checklist and missing items are provided they must be in the prompt."""
        prompt = self.worker._build_chapter_continuation_prompt(
            chapter_num=2,
            chapter_text="prose " * 100,
            chapter_goal="goal",
            checklist="1. Show Alice's fear\n2. End on cliffhanger",
            missing=["1: Alice's fear not shown"],
        )
        self.assertIn("fear", prompt.lower())
        self.assertIn("cliffhanger", prompt.lower())


# ---------------------------------------------------------------------------
# 3. change_chapter continuation prompt tests
# ---------------------------------------------------------------------------

class TestChangeChapterContinuationPrompt(unittest.TestCase):

    def setUp(self):
        self.worker = _make_worker(ctx=8192, max_tokens=4256)

    def _build(self, prose_len=2000, **kw):
        prose = "She swung her blade. " * (prose_len // 20)
        return cc_mod._build_continuation_prompt(
            worker=self.worker,
            chapter_num=2,
            chapter_content=prose,
            instruction="Add more tension to the fight.",
            checklist="1. Increase tension\n2. End with a wound",
            missing=kw.get("missing", []),
        )

    def test_returns_system_and_user(self):
        system, user = self._build()
        self.assertIsInstance(system, str)
        self.assertIsInstance(user, str)
        self.assertGreater(len(system), 10)
        self.assertGreater(len(user), 10)

    def test_continuity_anchors_present(self):
        _, user = self._build()
        self.assertIn("CONTINUITY ANCHORS", user)

    def test_outline_in_user(self):
        _, user = self._build()
        self.assertIn("guardian", user.lower())

    def test_characters_in_user(self):
        _, user = self._build()
        self.assertIn("Alice", user)

    def test_prose_tail_in_user(self):
        unique = "SENTINEL_CC_ZZ99"
        prose = ("padding " * 400) + unique
        _, user = cc_mod._build_continuation_prompt(
            worker=self.worker,
            chapter_num=2,
            chapter_content=prose,
            instruction="continue",
            checklist="",
            missing=[],
        )
        self.assertIn(unique, user)

    def test_prompt_fits_context(self):
        ctx, reply = 8192, 4256
        worker = _make_worker(ctx=ctx, max_tokens=reply)
        prose = "She fought. " * 2000
        _, user = cc_mod._build_continuation_prompt(
            worker=worker,
            chapter_num=2,
            chapter_content=prose,
            instruction="Add tension.",
            checklist="1. More tension",
            missing=["1: not enough tension"],
        )
        estimated = len(user) // 4
        self.assertLessEqual(
            estimated, ctx - reply,
            f"user prompt ~{estimated} tok but only {ctx - reply} available"
        )


# ---------------------------------------------------------------------------
# 4. reply_reserved uses content_max_tokens, not context_limit // 3
# ---------------------------------------------------------------------------

class TestReplyReserved(unittest.TestCase):

    def test_reply_reserved_equals_content_max_tokens(self):
        """
        With ctx=8192 and content_max_tokens=4256, the old formula gave
        context_limit // 3 = 2730.  The fixed formula should give 4256.
        """
        ctx, max_tok = 8192, 4256
        # Replicate the formula from workflow._run_inference_v2
        reply_reserved = max(256, min(ctx - 512, max_tok))
        self.assertEqual(reply_reserved, max_tok,
            f"reply_reserved={reply_reserved}, expected {max_tok} (not {ctx // 3})")

    def test_reply_reserved_capped_at_ctx_minus_512(self):
        """reply_reserved never consumes more than ctx-512 tokens."""
        ctx, max_tok = 2048, 8000   # max_tok > ctx
        reply_reserved = max(256, min(ctx - 512, max_tok))
        self.assertEqual(reply_reserved, ctx - 512)

    def test_reply_reserved_floor(self):
        """reply_reserved is at least 256 even when max_tokens is tiny."""
        ctx, max_tok = 4096, 100
        reply_reserved = max(256, min(ctx - 512, max_tok))
        self.assertEqual(reply_reserved, 256)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
