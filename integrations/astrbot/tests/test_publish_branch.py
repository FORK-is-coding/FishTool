"""发布脚本：孤儿首发、只追加更新、无变化跳过、令牌不进错误信息。用本地裸仓库，不访问网络。"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from release_tools.publish_branch import PublishError, auth_config, publish


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture()
def caller_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "caller"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.name", "Publisher", cwd=repo)
    _git("config", "user.email", "publisher@example.invalid", cwd=repo)
    return repo


@pytest.fixture()
def bare_remote(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    return remote


def _plugin_tree(root: Path, version: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "metadata.yaml").write_text(f"name: astrbot_plugin_fishtool\nversion: {version}\n", "utf-8")
    (root / "main.py").write_text("# plugin\n", "utf-8")
    (root / ".astrbot-plugin").mkdir(exist_ok=True)
    (root / ".astrbot-plugin" / "zh-CN.json").write_text("{}", "utf-8")
    return root


def test_orphan_then_append_then_noop(tmp_path: Path, caller_repo: Path, bare_remote: Path):
    url = str(bare_remote)
    tree = _plugin_tree(tmp_path / "v1", "0.1.0")
    sha1 = publish(tree, "main", "release 0.1.0", remote_url=url, repo=caller_repo)
    assert _git("rev-list", "--count", "main", cwd=bare_remote) == "1"
    assert _git("log", "-1", "--format=%an <%ae>", "main", cwd=bare_remote) == "Publisher <publisher@example.invalid>"
    files = set(_git("ls-tree", "-r", "--name-only", "main", cwd=bare_remote).splitlines())
    assert files == {"metadata.yaml", "main.py", ".astrbot-plugin/zh-CN.json"}

    tree2 = _plugin_tree(tmp_path / "v2", "0.1.1")
    (tree2 / "main.py").unlink()  # 删除的文件也要从发布分支消失
    sha2 = publish(tree2, "main", "release 0.1.1", remote_url=url, repo=caller_repo)
    assert sha2 != sha1
    assert _git("rev-parse", "main~1", cwd=bare_remote) == sha1  # 只追加，不改写历史
    files = set(_git("ls-tree", "-r", "--name-only", "main", cwd=bare_remote).splitlines())
    assert "main.py" not in files

    sha3 = publish(tree2, "main", "release 0.1.1 again", remote_url=url, repo=caller_repo)
    assert sha3 == sha2
    assert _git("rev-list", "--count", "main", cwd=bare_remote) == "2"


def test_dry_run_does_not_push(tmp_path: Path, caller_repo: Path, bare_remote: Path):
    tree = _plugin_tree(tmp_path / "v1", "0.1.0")
    publish(tree, "main", "dry", remote_url=str(bare_remote), repo=caller_repo, push=False)
    assert subprocess.run(["git", "rev-parse", "--verify", "main"], cwd=bare_remote, capture_output=True).returncode != 0


def test_rejects_non_plugin_tree(tmp_path: Path, caller_repo: Path, bare_remote: Path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(FileNotFoundError):
        publish(tmp_path / "empty", "main", "x", remote_url=str(bare_remote), repo=caller_repo)


def test_token_is_redacted_from_errors(tmp_path: Path, caller_repo: Path):
    token = "ghp_SECRET_TOKEN_VALUE_123"
    cfg, hidden = auth_config(token)
    assert token in hidden and len(hidden) == 2
    assert all(token not in item for item in cfg)  # 只出现 base64 形式
    tree = _plugin_tree(tmp_path / "v1", "0.1.0")
    missing = tmp_path / "does-not-exist.git"
    with pytest.raises(PublishError) as info:
        publish(tree, "main", "x", remote_url=str(missing), token=token, repo=caller_repo)
    message = str(info.value)
    for secret in hidden:
        assert secret not in message
