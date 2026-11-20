"""Tests explicitly authorize only their temporary import directory."""

import json
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def temporary_import_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAF_ALLOWED_SOURCE_ROOTS", json.dumps([str(tmp_path)]))
