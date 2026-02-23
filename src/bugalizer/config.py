"""Application configuration via environment variables."""

from __future__ import annotations

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

    model_config = {"env_prefix": "BUGALIZER_"}

    def valid_api_keys(self) -> set[str]:
        """Return the set of configured API keys (empty set = auth disabled)."""
        if not self.api_keys.strip():
            return set()
        return {k.strip() for k in self.api_keys.split(",") if k.strip()}


settings = Settings()
