import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def fixtures(tmp_path):
    """A private copy of the replay fixtures that a test may modify."""
    dst = tmp_path / "fixtures"
    shutil.copytree(ROOT / "fixtures", dst)
    return dst


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ALERT_SMTP_HOST", raising=False)
    monkeypatch.chdir(tmp_path)
