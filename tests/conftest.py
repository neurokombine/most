"""Общие приспособления для тестов моста. Сети нет ни в одном тесте."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bridge.config import Config, ChannelConfig          # noqa: E402
from bridge.store import Store                           # noqa: E402


@pytest.fixture()
def home(tmp_path):
    """Каталог экземпляра: ~/.most/<имя> в бою, tmp в тестах."""
    d = tmp_path / "most-home"
    d.mkdir()
    return d


@pytest.fixture()
def projects_dir(tmp_path):
    d = tmp_path / "projects"
    (d / "buhgalter").mkdir(parents=True)
    (d / "analitika").mkdir(parents=True)
    return d


@pytest.fixture()
def store(home):
    s = Store(home / "most.db")
    s.init()
    yield s
    s.close()


@pytest.fixture()
def config(home, projects_dir):
    return Config(
        name="test",
        home=home,
        config_path=home / "config.yaml",
        projects_dir=projects_dir,
        executor="claude",
        telegram=ChannelConfig(token="tg-token-secret", allowlist=[111]),
        max=ChannelConfig(token="max-token-secret", allowlist=[222]),
    )
