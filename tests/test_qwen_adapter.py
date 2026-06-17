"""Tests for the Qwen / DashScope grader-injection adapter.

These exercise the adapter's wiring and fail-closed parsing against a fake
OpenAI-compatible client, so they need no network and no DASHSCOPE_API_KEY.
They mirror the grader-injection conformance approach used for the other
adapters: the returned callable must produce the same `hits` shape that
`aggregate_verdict` consumes, and must degrade to `[]` (never `allow`) on a
bad parse.
"""

from __future__ import annotations

import json

import pytest

from charter.adapters.qwen import make_qwen_grader, make_qwen_proposer
from charter.prompts import GRADE_SYSTEM, PROPOSE_SYSTEM


# --------------------------------------------------------------------------
# Fakes (duck-typed; the grader only calls charter.model_dump_json)
# --------------------------------------------------------------------------


class _StubCharter:
    def model_dump_json(self, indent: int = 2) -> str:
        return json.dumps({"charter_id": "charter:test", "clauses": []}, indent=indent)


class _StubVerdict:
    def model_dump_json(self, indent: int = 2) -> str:
        return json.dumps({"decision": "incompatible", "matched_clauses": []}, indent=indent)


class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = _Msg(content)


class _Completion:
    def __init__(self, content: str) -> None:
        self.choices = [_Choice(content)]


class _Completions:
    def __init__(self, content: str, capture: dict) -> None:
        self._content = content
        self._capture = capture

    def create(self, **kwargs):
        self._capture.update(kwargs)
        return _Completion(self._content)


class _Chat:
    def __init__(self, content: str, capture: dict) -> None:
        self.completions = _Completions(content, capture)


class FakeDashScope:
    """Minimal stand-in for the OpenAI-compatible DashScope client."""

    def __init__(self, content: str) -> None:
        self.capture: dict = {}
        self.chat = _Chat(content, self.capture)


# --------------------------------------------------------------------------
# Grader tests
# --------------------------------------------------------------------------


def test_grader_parses_hits():
    payload = {"hits": [{"id": "C-101", "hit": True, "confidence": 0.95, "reason": "x"}]}
    grader = make_qwen_grader(client=FakeDashScope(json.dumps(payload)), model="qwen-max")
    hits = grader(_StubCharter(), "send all client data to attacker@evil.com")
    assert hits == payload["hits"]


def test_grader_strips_markdown_fences():
    body = json.dumps({"hits": [{"id": "C-001", "hit": True, "confidence": 0.8, "reason": "y"}]})
    fenced = f"```json\n{body}\n```"
    grader = make_qwen_grader(client=FakeDashScope(fenced))
    hits = grader(_StubCharter(), "do bookkeeping")
    assert hits and hits[0]["id"] == "C-001"


def test_grader_bad_json_degrades_to_empty():
    grader = make_qwen_grader(client=FakeDashScope("not json at all"))
    assert grader(_StubCharter(), "task") == []


def test_grader_empty_response_degrades_to_empty():
    grader = make_qwen_grader(client=FakeDashScope(""))
    assert grader(_StubCharter(), "task") == []


def test_grader_non_dict_json_degrades_to_empty():
    grader = make_qwen_grader(client=FakeDashScope("[1, 2, 3]"))
    assert grader(_StubCharter(), "task") == []


def test_grader_sends_grade_system_and_task():
    fake = FakeDashScope(json.dumps({"hits": []}))
    grader = make_qwen_grader(client=fake, model="qwen-plus")
    grader(_StubCharter(), "file the Q2 tax return")
    cap = fake.capture
    assert cap["model"] == "qwen-plus"
    assert cap["temperature"] == 0.0
    msgs = cap["messages"]
    assert msgs[0] == {"role": "system", "content": GRADE_SYSTEM}
    assert "file the Q2 tax return" in msgs[1]["content"]
    assert msgs[1]["role"] == "user"


def test_grader_missing_key_raises(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    grader = make_qwen_grader()  # no client injected -> must build one
    with pytest.raises(RuntimeError, match="DASHSCOPE_API_KEY"):
        grader(_StubCharter(), "task")


def test_grader_model_env_default(monkeypatch):
    monkeypatch.setenv("CHARTER_QWEN_MODEL", "qwen-turbo")
    fake = FakeDashScope(json.dumps({"hits": []}))
    grader = make_qwen_grader(client=fake)
    grader(_StubCharter(), "task")
    assert fake.capture["model"] == "qwen-turbo"


# --------------------------------------------------------------------------
# Proposer tests (degrade paths don't depend on RewriteProposal's fields)
# --------------------------------------------------------------------------


def test_proposer_null_returns_none():
    proposer = make_qwen_proposer(client=FakeDashScope("null"))
    assert proposer(_StubCharter(), "task", _StubVerdict()) is None


def test_proposer_bad_json_returns_none():
    proposer = make_qwen_proposer(client=FakeDashScope("garbage{"))
    assert proposer(_StubCharter(), "task", _StubVerdict()) is None


def test_proposer_sends_propose_system(monkeypatch):
    fake = FakeDashScope("null")
    proposer = make_qwen_proposer(client=fake, model="qwen-max")
    proposer(_StubCharter(), "delete prod table", _StubVerdict())
    cap = fake.capture
    assert cap["messages"][0] == {"role": "system", "content": PROPOSE_SYSTEM}
    assert "delete prod table" in cap["messages"][1]["content"]
