"""Shared test environment — imported by pytest before any test module.

Since §5.5, `Settings` reads a `.env` file from the working directory
(native-service deploys). Explicit environment variables always beat the
`.env` file, so pin the values the suite depends on here — a developer's
real `.env` in the repo root must not flip auth on, point at a real DB,
or start the queue worker mid-test.
"""

import os

os.environ.setdefault("BUGALIZER_DB_PATH", ":memory:")
os.environ["BUGALIZER_QUEUE_ENABLED"] = "false"
os.environ["BUGALIZER_API_KEYS"] = ""       # auth disabled in tests
os.environ["BUGALIZER_CORS_ORIGINS"] = ""   # CORS closed unless a test opts in
