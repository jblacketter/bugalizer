"""Shared test environment — imported by pytest before any test module.

The suite must run against the shipped code defaults, never against a local
deployment's configuration. Two mechanisms can leak that config in:

1. A `.env` file in the repo root (the app reads it in production, §5.5) — and
   `uv run` loads it straight into the process environment, so its keys arrive
   as real `BUGALIZER_*` env vars that beat everything.
2. The machine's ambient env (a dev box exports `QA_LLM_MODEL=ollama/...` for
   local inference, which the generic fallback turns into the fix provider).

So: strip every `BUGALIZER_*` and `QA_LLM_*` var first, then pin only the ones
the suite needs. Individual tests set their own overrides via monkeypatch.
"""

import os

import pytest


def _pin_test_env() -> None:
    """Strip every deploy/ambient BUGALIZER_*/QA_LLM_* var, then pin the suite's
    fixed values. Repeatable because it must run more than once (see fixture)."""
    for _key in list(os.environ):
        if _key.startswith("BUGALIZER_") or _key.startswith("QA_LLM_"):
            del os.environ[_key]
    # Stop pydantic reading the repo-root `.env` directly (config.py maps an
    # empty BUGALIZER_ENV_FILE to no env file).
    os.environ["BUGALIZER_ENV_FILE"] = ""
    os.environ["BUGALIZER_DB_PATH"] = ":memory:"
    os.environ["BUGALIZER_QUEUE_ENABLED"] = "false"
    os.environ["BUGALIZER_API_KEYS"] = ""       # auth disabled in tests
    os.environ["BUGALIZER_CORS_ORIGINS"] = ""   # CORS closed unless a test opts in


# Run once at import so the config singleton is built against clean env...
_pin_test_env()


@pytest.fixture(autouse=True)
def _isolate_env():
    """...and again before every test. `uv run`, pydantic, and litellm's
    `load_dotenv()` (on import) all reload the repo-root deploy `.env` into the
    process environment, so a single session-level strip is undone the moment an
    LLM module is imported. Re-pinning per test keeps a fresh Settings() reading
    shipped defaults; individual tests layer overrides via monkeypatch."""
    _pin_test_env()
    yield
