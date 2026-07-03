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


# ---------------------------------------------------------------------------
# Provider/model resolution (Phase 5 §5.3)
#
# Precedence for every stage: per-project override → global setting.
# LOCAL stages (triage, localization) read the project's `llm_provider` /
# `llm_model`; Stage 4 (fix proposals) reads `fix_llm_provider` /
# `fix_llm_model` and NEVER the local pair — there is no path by which a
# project's `llm_provider=ollama` reaches Stage 4.
# ---------------------------------------------------------------------------

def resolve_local_llm(
    project: Optional[dict[str, Any]], stage: str
) -> tuple[str, str]:
    """Resolve (provider, model) for a local pipeline stage.

    `stage` is "triage" or "localize" — it selects which global default
    model applies when the project carries no value. Projects default to
    `ollama` / `qwen2.5-coder:7b` (same as the globals), so a default
    project keeps exactly the pre-§5.3 local behavior.
    """
    default_model = (
        settings.default_triage_model
        if stage == "triage"
        else settings.default_localize_model
    )
    provider = "ollama"
    model = default_model
    if project:
        if project.get("llm_provider"):
            provider = project["llm_provider"]
        if project.get("llm_model"):
            model = project["llm_model"]
    return provider, model


def resolve_fix_llm(project: Optional[dict[str, Any]]) -> tuple[str, str]:
    """Resolve (provider, model) for Stage 4 fix proposals.

    Per-field fallback: `fix_llm_provider` → global `fix_provider`;
    `fix_llm_model` → global `default_fix_model`. Setting only the model
    keeps the global provider (e.g. pin a different Claude model while
    staying on `anthropic`).
    """
    provider = settings.fix_provider
    model = settings.default_fix_model
    if project:
        if project.get("fix_llm_provider"):
            provider = project["fix_llm_provider"]
        if project.get("fix_llm_model"):
            model = project["fix_llm_model"]
    return provider, model


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
        # Full-string passthrough (docs/phases/architecture.md, "LLM tiering"):
        # providers beyond ollama|anthropic route via the litellm model string verbatim;
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
