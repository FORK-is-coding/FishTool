from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_compose_publishes_loopback_by_default():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text("utf-8"))
    ports = compose["services"]["fishtool"]["ports"]

    assert ports == ["${FISHTOOL_BIND_ADDRESS:-127.0.0.1}:8000:8000"]


def test_dockerignore_does_not_ship_env_files():
    lines = (ROOT / ".dockerignore").read_text("utf-8").splitlines()

    assert ".env" in lines
    assert ".env.*" in lines
