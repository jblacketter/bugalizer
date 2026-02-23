"""Thin wrapper around litellm for calling Ollama models."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import litellm

from bugalizer.config import settings

logger = logging.getLogger(__name__)

# Suppress litellm's verbose logging.
litellm.suppress_debug_info = True


@dataclass
class LLMResponse:
    """Structured response from an LLM call."""
    content: str
    prompt_tokens: int
    completion_tokens: int
    model: str
    provider: str


async def complete(
    model: Optional[str] = None,
    messages: list[dict[str, str]] | None = None,
    *,
    api_base: Optional[str] = None,
    timeout: int = 120,
) -> LLMResponse:
    """Call an LLM via litellm (Ollama by default).

    Args:
        model: Model identifier (e.g. "ollama/qwen2.5-coder:7b").
        messages: Chat messages in OpenAI format.
        api_base: Override Ollama host URL.
        timeout: Request timeout in seconds.

    Returns:
        LLMResponse with content, token counts, and model info.
    """
    if model is None:
        model = f"ollama/{settings.default_triage_model}"
    if not model.startswith("ollama/"):
        model = f"ollama/{model}"

    if api_base is None:
        api_base = settings.ollama_host

    if messages is None:
        messages = []

    logger.debug("LLM call: model=%s, messages=%d", model, len(messages))

    response = await litellm.acompletion(
        model=model,
        messages=messages,
        api_base=api_base,
        timeout=timeout,
    )

    content = response.choices[0].message.content or ""
    usage = response.usage
    prompt_tokens = usage.prompt_tokens if usage else 0
    completion_tokens = usage.completion_tokens if usage else 0

    return LLMResponse(
        content=content,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        model=model,
        provider="ollama",
    )
