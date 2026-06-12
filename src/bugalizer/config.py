"""Application configuration via environment variables."""

from __future__ import annotations

import os

from pydantic import model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Bugalizer server settings.

    All values can be overridden via environment variables (prefixed BUGALIZER_)
    or a .env file.
    """

    host: str = "0.0.0.0"
    port: int = 8090
    debug: bool = False

    # Comma-separated list of valid API keys for X-API-Key auth.
    api_keys: str = ""

    # SQLite database path (relative to cwd or absolute).
    db_path: str = "bugalizer.db"

    # Ollama
    ollama_host: str = "http://localhost:11434"
    default_triage_model: str = "qwen2.5-coder:7b"

    # Queue worker
    queue_poll_seconds: int = 5
    queue_max_concurrent: int = 2
    queue_enabled: bool = True

    # Pipeline
    duplicate_threshold: float = 0.8
    retry_delay_seconds: int = 60
    max_triage_retries: int = 3

    # Git repos
    repos_dir: str = "./repos"

    # Cache
    cache_dir: str = "./cache"

    # Localization (Stage 3)
    default_localize_model: str = "qwen2.5-coder:7b"
    repo_map_max_files: int = 50
    repo_map_max_tokens: int = 4000
    repo_map_ttl_hours: int = 24
    localize_max_file_chars: int = 8000
    localize_max_files: int = 3
    localize_confidence_threshold: float = 0.5

    # Encryption key for stored LLM API keys (Fernet, base64-encoded 32 bytes).
    secret_key: str = ""

    # Fix proposals (Stage 4 / bugalizer Phase 4) — cloud LLM via litellm.
    default_fix_model: str = "claude-sonnet-4-6"
    fix_provider: str = "anthropic"
    anthropic_api_key: str = ""
    fix_max_bundle_bytes: int = 4_194_304   # 4 MiB total file-bundle cap
    fix_max_file_bytes: int = 524_288       # 512 KiB per-file cap
    fix_enable_prompt_caching: bool = True

    model_config = {"env_prefix": "BUGALIZER_"}

    @model_validator(mode="after")
    def _apply_generic_llm_fallbacks(self) -> "Settings":
        """docs/llm-tiering.md: QA_LLM_* generic env as a fallback layer.

        BUGALIZER_* settings (any source — env, .env, init kwargs) always
        win; `model_fields_set` is the explicitly-configured check. The fix
        model and provider fall back atomically — a pinned fix_provider
        blocks the generic model so the two are never mismatched. Triage/
        localize are local-only: they consume QA_LLM_MODEL only when it is
        an `ollama/` string; a cloud model string leaves them untouched.
        """
        generic = os.environ.get("QA_LLM_MODEL")
        if generic:
            if (
                "fix_provider" not in self.model_fields_set
                and "default_fix_model" not in self.model_fields_set
            ):
                self.default_fix_model = generic
                self.fix_provider = (
                    generic.partition("/")[0] if "/" in generic else "openai"
                )
            if generic.startswith("ollama/"):
                bare = generic.removeprefix("ollama/")
                if "default_triage_model" not in self.model_fields_set:
                    self.default_triage_model = bare
                if "default_localize_model" not in self.model_fields_set:
                    self.default_localize_model = bare
        generic_base = os.environ.get("QA_LLM_API_BASE")
        if generic_base and "ollama_host" not in self.model_fields_set:
            self.ollama_host = generic_base
        return self

    def valid_api_keys(self) -> set[str]:
        """Return the set of configured API keys (empty set = auth disabled)."""
        if not self.api_keys.strip():
            return set()
        return {k.strip() for k in self.api_keys.split(",") if k.strip()}


settings = Settings()
