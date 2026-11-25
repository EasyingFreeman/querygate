from pathlib import Path

import pytest
import yaml

from querygate.config import Settings


@pytest.fixture
def settings():
    path = Path(__file__).parents[1] / "querygate.example.yaml"
    return Settings.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
