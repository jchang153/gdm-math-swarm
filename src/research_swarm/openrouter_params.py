"""Send OpenRouter's ``max_tokens`` for every model, including OpenAI reasoning models.

Inspect 0.3.279 renames ``max_tokens`` to ``max_completion_tokens`` for GPT-5 and o-series
model families (``needs_max_completion_tokens``), also on OpenRouter. OpenRouter's pinned
``openai`` route for ``openai/gpt-5.6-sol`` lists ``max_tokens`` but not
``max_completion_tokens`` among its supported parameters, and the swarm requests set
``require_parameters``, so the renamed field could make OpenRouter refuse the pinned route.
``provider.validate_catalog`` already requires ``max_tokens`` on every pinned route, and the
reservation bound assumes exactly that cap. This wrapper only undoes the rename; other
models' requests (which already carry ``max_tokens``) are unchanged.
"""
from __future__ import annotations

import functools
import importlib.metadata
import threading

_EXPECTED_INSPECT_VERSION = "0.3.279"
_LOCK = threading.Lock()
_INSTALLED = False


def install_openrouter_max_tokens():
    """Install once (idempotent, version-guarded); no network or keys."""
    global _INSTALLED
    with _LOCK:
        if importlib.metadata.version("inspect-ai") != _EXPECTED_INSPECT_VERSION:
            raise RuntimeError("OpenRouter max_tokens patch requires inspect-ai " + _EXPECTED_INSPECT_VERSION)
        if _INSTALLED:
            return
        from inspect_ai.model._providers.openrouter import OpenRouterAPI
        original = OpenRouterAPI.completion_params

        @functools.wraps(original)
        def completion_params(self, config, tools):
            params = original(self, config, tools)
            if "max_completion_tokens" in params and "max_tokens" not in params:
                params["max_tokens"] = params.pop("max_completion_tokens")
            return params

        OpenRouterAPI.completion_params = completion_params
        _INSTALLED = True
