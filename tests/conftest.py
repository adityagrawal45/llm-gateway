from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Must be set before llm_gateway.main is imported, since main.py reads
# CONFIG_PATH at module load time.
REPO_ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("CONFIG_PATH", str(REPO_ROOT / "config" / "config.yaml"))


@pytest.fixture()
def client() -> TestClient:
    from llm_gateway.main import create_app

    app = create_app()
    with TestClient(app) as test_client:
        yield test_client
