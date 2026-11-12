from __future__ import annotations

from engine.models import Project, TaskType
from engine import workflow


class _StreamingEngine:
    def __init__(self, response: str, chunks: list[str]) -> None:
        self.response = response
        self.chunks = chunks
        self.kwargs = None

    def generate(self, **kwargs) -> str:
        self.kwargs = kwargs
        assert kwargs["stream"] is True
        callback = kwargs["stream_callback"]
        for chunk in self.chunks:
            callback(chunk)
        return self.response


def _make_worker() -> workflow.WorkflowWorker:
    return workflow.WorkflowWorker(
        project=Project(title="Streaming test"),
        task=TaskType.GENERATE_OUTLINE,
    )


def test_lean_inference_forwards_streamed_tokens(monkeypatch):
    worker = _make_worker()
    engine = _StreamingEngine("A complete response.", ["A ", "complete ", "response."])
    received: list[str] = []

    monkeypatch.setattr(worker, "_load_model_for_task", lambda _task: True)
    monkeypatch.setattr(worker, "_model_context_limit", lambda: 4096)
    monkeypatch.setattr(worker, "_task_temperature", lambda _task: 0.7)
    monkeypatch.setattr(worker, "_task_top_p", lambda _task: 0.9)
    monkeypatch.setattr(worker, "_task_top_k", lambda _task: 40)
    monkeypatch.setattr(workflow, "estimate_messages_tokens", lambda _messages: 10)
    monkeypatch.setattr(workflow, "get_engine", lambda: engine)
    worker.token_received.connect(received.append)

    result = worker._run_lean_inference(
        TaskType.GENERATE_OUTLINE,
        "system instructions",
        "user instructions",
        max_tokens=128,
    )

    assert result == "A complete response."
    assert received == ["A ", "complete ", "response."]
    assert engine.kwargs["stream_callback"] is not None


def test_character_extraction_forwards_streamed_tokens(monkeypatch):
    worker = _make_worker()
    engine = _StreamingEngine('{"characters":[]}', ["{", '"characters"', ":[]", "}"])
    received: list[str] = []

    monkeypatch.setattr(worker, "_load_model_for_task", lambda _task: True)
    monkeypatch.setattr(worker, "_model_context_limit", lambda: 4096)
    monkeypatch.setattr(workflow, "estimate_messages_tokens", lambda _messages: 10)
    monkeypatch.setattr(workflow, "get_engine", lambda: engine)
    monkeypatch.setattr(workflow.prompts, "render", lambda *_args, **_kwargs: "prompt")
    monkeypatch.setattr(workflow.prompts, "load_raw", lambda *_args, **_kwargs: "system")
    monkeypatch.setattr(worker, "_merge_extracted_characters", lambda _raw: 0)
    worker.token_received.connect(received.append)

    worker._extract_and_merge_characters("A story about two characters.")

    assert received == ["{", '"characters"', ":[]", "}"]
    assert engine.kwargs["stream_callback"] is not None
