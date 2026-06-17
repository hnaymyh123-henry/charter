"""Charter adapter for Qwen on Alibaba Cloud (DashScope / Model Studio).

This module lets the Charter Compatibility Check run entirely on **Qwen
Cloud** instead of Anthropic, using the exact same grader-injection seam as
:mod:`charter.adapters.openai_agents`:

  - :func:`make_qwen_grader` returns a ``HitsGrader``
    (``(Charter, str) -> list[dict]``) you can pass as ``hits_grader=`` to
    ``charter_preflight`` / ``charter_gated`` / the Postgres gate / the CFO
    Office orchestrator. It mirrors :func:`charter.loopback._grade_via_llm`
    byte-for-byte in prompt, framing, and parse-and-degrade-to-``[]``
    semantics — only the provider behind it changes.
  - :func:`make_qwen_proposer` returns a callable matching
    :func:`charter.propose.propose_within_scope_llm` for the scope-rewrite
    path, driven by ``PROPOSE_SYSTEM``.

**Zero protocol change.** ``TYPE_TO_DECISION``, the aggregation precedence,
and the ``Verdict`` shape are untouched; this is purely an LLM backend swap,
which is exactly why "swap to Qwen without touching the protocol" is provable.

Alibaba Cloud service call
--------------------------
This file is the project's *deployment-proof artifact*: it calls the Alibaba
Cloud DashScope service through its OpenAI-compatible endpoint
(``https://dashscope.aliyuncs.com/compatible-mode/v1``; use the ``-intl`` host
outside mainland China). Configure via env:

  - ``DASHSCOPE_API_KEY``   — required (your Alibaba Cloud Model Studio key)
  - ``CHARTER_QWEN_MODEL``  — optional, default ``qwen-max``
  - ``DASHSCOPE_BASE_URL``  — optional, override the endpoint

Install the client with ``pip install charter[qwen]`` (DashScope speaks the
OpenAI wire protocol, so the ``openai`` package is the only extra needed).
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any

from .._logging import get_logger
from ..prompts import GRADE_SYSTEM, PROPOSE_SYSTEM
from ..propose import _strip_markdown_fences
from ..schema import Charter, RewriteProposal, Verdict

_log = get_logger("charter.adapters.qwen")

# Same alias the other adapters export, so the seam is type-compatible.
HitsGrader = Callable[[Charter, str], list[dict[str, Any]]]

DEFAULT_QWEN_MODEL = "qwen-max"
DEFAULT_DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

_MAX_TOKENS = 1024


# ---------------------------------------------------------------------------
# DashScope (Alibaba Cloud) client plumbing
# ---------------------------------------------------------------------------


def _make_client(api_key: str | None = None, base_url: str | None = None) -> Any:
    """Build an OpenAI-compatible client pointed at Alibaba Cloud DashScope.

    Imported lazily so importing this module does not require ``openai`` until
    a caller actually grades against Qwen. Raises ``RuntimeError`` with an
    actionable message when the package or the API key is missing — mirroring
    how ``loopback._grade_via_llm`` raises on a missing ``ANTHROPIC_API_KEY``.
    """
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise RuntimeError(
            "The 'openai' package is required for the Qwen adapter "
            "(DashScope uses the OpenAI-compatible API). "
            "Install it with `pip install charter[qwen]`."
        ) from exc

    key = api_key or os.environ.get("DASHSCOPE_API_KEY")
    if not key:
        raise RuntimeError(
            "DASHSCOPE_API_KEY is not set; the Qwen adapter cannot call DashScope."
        )
    url = base_url or os.environ.get("DASHSCOPE_BASE_URL", DEFAULT_DASHSCOPE_BASE_URL)
    return OpenAI(api_key=key, base_url=url)


def _chat(client: Any, model: str, system: str, user: str, temperature: float) -> str:
    """One OpenAI-compatible chat completion against DashScope; return text."""
    resp = client.chat.completions.create(
        model=model,
        temperature=temperature,
        max_tokens=_MAX_TOKENS,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return resp.choices[0].message.content or ""


def _resolve_model(model: str | None) -> str:
    return model or os.environ.get("CHARTER_QWEN_MODEL", DEFAULT_QWEN_MODEL)


# ---------------------------------------------------------------------------
# Public factories
# ---------------------------------------------------------------------------


def make_qwen_grader(
    *,
    model: str | None = None,
    client: Any | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> HitsGrader:
    """Return a ``HitsGrader`` that grades clause-hits with Qwen on DashScope.

    The returned callable is a drop-in for the default Anthropic grader
    (:func:`charter.loopback._grade_via_llm`): same ``GRADE_SYSTEM`` prompt,
    same ``Charter`` + ``intended_task`` framing, ``temperature=0.0``, and the
    same fail-closed parsing — any empty/unparseable response yields ``[]``,
    which makes ``aggregate_verdict`` fall back to its zero-match default
    (``needs_approval``). It never silently upgrades to ``allow`` on a bad parse.

    Args:
        model:    Qwen model id. Default: env ``CHARTER_QWEN_MODEL`` or
                  ``qwen-max``.
        client:   Pre-built OpenAI-compatible client (used by tests / custom
                  gateways). When ``None``, one is built from
                  ``DASHSCOPE_API_KEY`` (+ optional ``DASHSCOPE_BASE_URL``)
                  lazily on first call.
        api_key:  Override ``DASHSCOPE_API_KEY``.
        base_url: Override the DashScope endpoint.

    Returns:
        ``(charter, intended_task) -> hits`` suitable as ``hits_grader=``
        anywhere in Charter.
    """

    def grade(charter: Charter, intended_task: str) -> list[dict[str, Any]]:
        cli = client if client is not None else _make_client(api_key, base_url)
        model_id = _resolve_model(model)
        charter_json = charter.model_dump_json(indent=2)
        user = f"Charter:\n```json\n{charter_json}\n```\n\nintended_task:\n{intended_task}"

        raw = _chat(cli, model_id, GRADE_SYSTEM, user, temperature=0.0)
        text = _strip_markdown_fences(raw)
        if not text:
            return []
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return []
        if not isinstance(data, dict):
            return []
        hits = data.get("hits", [])
        return hits if isinstance(hits, list) else []

    return grade


def make_qwen_proposer(
    *,
    model: str | None = None,
    client: Any | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> Callable[..., RewriteProposal | None]:
    """Return a scope-rewrite proposer backed by Qwen on DashScope.

    Matches the signature and contract of
    :func:`charter.propose.propose_within_scope_llm`: returns a
    ``RewriteProposal`` on success, or ``None`` when the model declines
    (literal ``null``) or its output cannot be parsed/validated.

    Args mirror :func:`make_qwen_grader`.
    """

    def propose(
        charter: Charter,
        intended_task: str,
        failed_verdict: Verdict,
        *,
        temperature: float = 0.2,
        extra_user_context: str | None = None,
    ) -> RewriteProposal | None:
        cli = client if client is not None else _make_client(api_key, base_url)
        model_id = _resolve_model(model)

        charter_json = charter.model_dump_json(indent=2)
        verdict_json = failed_verdict.model_dump_json(indent=2)
        user = (
            f"Charter:\n```json\n{charter_json}\n```\n\n"
            f"intended_task:\n{intended_task}\n\n"
            f"failed_verdict:\n```json\n{verdict_json}\n```"
        )
        if extra_user_context:
            user += f"\n\n{extra_user_context}"

        raw = _chat(cli, model_id, PROPOSE_SYSTEM, user, temperature=temperature)
        text = _strip_markdown_fences(raw)
        if not text or text == "null":
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        if data is None:
            return None
        try:
            return RewriteProposal.model_validate(data)
        except Exception:
            return None

    return propose


__all__ = [
    "HitsGrader",
    "DEFAULT_QWEN_MODEL",
    "DEFAULT_DASHSCOPE_BASE_URL",
    "make_qwen_grader",
    "make_qwen_proposer",
]
