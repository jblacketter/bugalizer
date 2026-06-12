"""Thin wrapper around litellm for calling Ollama + Anthropic models."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

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
    messages: list[dict[str, Any]] | None = None,
    *,
    api_base: Optional[str] = None,
    timeout: int = 120,
    provider: str = "ollama",
    api_key: Optional[str] = None,
) -> LLMResponse:
    """Call an LLM via litellm.

    Args:
        model: Model identifier. Shape depends on provider.
        messages: Chat messages in OpenAI format. `content` may be either a
            plain string or a list of content parts (Anthropic-style) when
            the caller wants to attach per-part metadata like cache_control.
        api_base: Override host URL (Ollama only).
        timeout: Request timeout in seconds.
        provider: `"ollama"` (default, local) or `"anthropic"` (cloud via
            litellm).
        api_key: Explicit API key, otherwise pulled from config.

    Returns:
        LLMResponse with content, token counts, and model info.
    """
    if messages is None:
        messages = []

    kwargs: dict[str, Any] = {
        "messages": messages,
        "timeout": timeout,
    }

    if provider == "ollama":
        if model is None:
            model = f"ollama/{settings.default_triage_model}"
        if not model.startswith("ollama/"):
            model = f"ollama/{model}"
        kwargs["api_base"] = api_base or settings.ollama_host
        resolved_model = model
    elif provider == "anthropic":
        if model is None:
            model = settings.default_fix_model
        # litellm accepts bare claude model ids directly; also accepts
        # the `anthropic/` prefix. Normalize to the prefixed form for clarity.
        if not model.startswith("anthropic/"):
            resolved_model = f"anthropic/{model}"
        else:
            resolved_model = model
        key = api_key or settings.anthropic_api_key
        if not key:
            raise RuntimeError(
                "anthropic provider selected but BUGALIZER_ANTHROPIC_API_KEY is not set. "
                "Set the env var or pass api_key= explicitly."
            )
        kwargs["api_key"] = key
    else:
        # docs/llm-tiering.md full-string passthrough: providers beyond
        # ollama|anthropic route via the litellm model string verbatim;
        # credentials come from provider-native env vars (litellm reads
        # OPENAI_API_KEY etc. itself).
        if model is None:
            raise ValueError(
                f"provider {provider!r} requires an explicit litellm model string"
            )
        resolved_model = model
        if api_key:
            kwargs["api_key"] = api_key

    kwargs["model"] = resolved_model
    logger.debug("LLM call: provider=%s model=%s messages=%d",
                 provider, resolved_model, len(messages))

    response = await litellm.acompletion(**kwargs)

    content = response.choices[0].message.content or ""
    usage = response.usage
    prompt_tokens = usage.prompt_tokens if usage else 0
    completion_tokens = usage.completion_tokens if usage else 0

    return LLMResponse(
        content=content,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        model=resolved_model,
        provider=provider,
    )
