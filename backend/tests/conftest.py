"""Establish isolated test configuration before any application module is imported."""

import os
import tempfile
from pathlib import Path

if os.getenv("TEST_DATABASE") == "postgres":
    os.environ["DATABASE_URL"] = os.environ["POSTGRES_TEST_URL"]
else:
    os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + str(
        Path(tempfile.mkdtemp(prefix="avatar-tests-")) / "test.db"
    )
os.environ["PIPELINE_MODE"] = "mock"
os.environ["PIPELINE_PROFILE"] = "openai-asr-kokoro-tts"
os.environ["API_ACCESS_KEY"] = "test-secret"
os.environ["MOCK_TTS_PREVIEW"] = "false"
