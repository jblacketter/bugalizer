"""docs/llm-tiering.md: QA_LLM_* generic fallbacks in Settings."""

from __future__ import annotations

from bugalizer.config import Settings


def test_no_generic_env_unchanged(monkeypatch):
    monkeypatch.delenv("QA_LLM_MODEL", raising=False)
    monkeypatch.delenv("QA_LLM_API_BASE", raising=False)
    s = Settings(_env_file=None)
    assert s.ollama_host == "http://localhost:11434"
    assert s.default_triage_model == "qwen2.5-coder:7b"
    assert s.default_fix_model == "claude-sonnet-4-6"
    assert s.fix_provider == "anthropic"


def test_generic_ollama_model_feeds_local_and_fix(monkeypatch):
    monkeypatch.setenv("QA_LLM_MODEL", "ollama/qwen2.5-coder:14b")
    monkeypatch.setenv("QA_LLM_API_BASE", "http://gpu-box:11434")
    s = Settings(_env_file=None)
    assert s.default_triage_model == "qwen2.5-coder:14b"  # bare name
    assert s.default_localize_model == "qwen2.5-coder:14b"
    assert s.ollama_host == "http://gpu-box:11434"
    assert s.default_fix_model == "ollama/qwen2.5-coder:14b"  # full string
    assert s.fix_provider == "ollama"


def test_generic_cloud_model_leaves_local_tiers_untouched(monkeypatch):
    """Local-only rule: a non-ollama string never alters triage/localize."""
    monkeypatch.setenv("QA_LLM_MODEL", "openai/gpt-4o-mini")
    s = Settings(_env_file=None)
    assert s.default_triage_model == "qwen2.5-coder:7b"
    assert s.default_localize_model == "qwen2.5-coder:7b"
    assert s.default_fix_model == "openai/gpt-4o-mini"
    assert s.fix_provider == "openai"


def test_pinned_fix_provider_blocks_generic_model(monkeypatch):
    """The misroute repro: an explicitly-set fix_provider must not be
    combined with a generic model string (atomic fallback)."""
    monkeypatch.setenv("QA_LLM_MODEL", "openai/gpt-4o-mini")
    monkeypatch.setenv("BUGALIZER_FIX_PROVIDER", "anthropic")
    s = Settings(_env_file=None)
    assert s.fix_provider == "anthropic"
    assert s.default_fix_model == "claude-sonnet-4-6"  # NOT gpt-4o-mini


def test_bugalizer_env_wins_over_generic(monkeypatch):
    monkeypatch.setenv("QA_LLM_MODEL", "ollama/qwen2.5-coder:14b")
    monkeypatch.setenv("QA_LLM_API_BASE", "http://generic:11434")
    monkeypatch.setenv("BUGALIZER_DEFAULT_TRIAGE_MODEL", "llama3.1")
    monkeypatch.setenv("BUGALIZER_OLLAMA_HOST", "http://service:11434")
    s = Settings(_env_file=None)
    assert s.default_triage_model == "llama3.1"
    assert s.ollama_host == "http://service:11434"


def test_bare_generic_string_is_openai(monkeypatch):
    monkeypatch.setenv("QA_LLM_MODEL", "gpt-4o-mini")
    s = Settings(_env_file=None)
    assert s.fix_provider == "openai"
    assert s.default_fix_model == "gpt-4o-mini"


def test_env_file_is_read_and_real_env_wins(tmp_path, monkeypatch):
    """§5.5: Settings reads a .env file (native-service deploys); a real
    environment variable always beats the same key in the file."""
    monkeypatch.delenv("QA_LLM_MODEL", raising=False)
    monkeypatch.delenv("QA_LLM_API_BASE", raising=False)
    envfile = tmp_path / ".env"
    envfile.write_text("BUGALIZER_PORT=9999\nBUGALIZER_DEBUG=true\n", encoding="utf-8")

    s = Settings(_env_file=str(envfile))
    assert s.port == 9999
    assert s.debug is True

    monkeypatch.setenv("BUGALIZER_PORT", "7070")
    s = Settings(_env_file=str(envfile))
    assert s.port == 7070  # env var wins over .env
