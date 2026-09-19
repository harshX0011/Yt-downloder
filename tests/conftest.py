from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

# Settings are read once per process, so the environment has to be in place
# before `backend.config.get_settings` is first called.
os.environ.setdefault("DOWNLOAD_DIR", "")


@pytest.fixture(scope="session", autouse=True)
def _isolated_download_dir(tmp_path_factory) -> Iterator[None]:
    directory = tmp_path_factory.mktemp("downloads")
    os.environ["DOWNLOAD_DIR"] = str(directory)
    os.environ["RATE_LIMIT_REQUESTS"] = "1000"
    from backend.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def settings():
    from backend.config import get_settings

    return get_settings()


@pytest.fixture
def client() -> Iterator:
    from fastapi.testclient import TestClient

    from backend.main import app

    with TestClient(app) as test_client:
        yield test_client
