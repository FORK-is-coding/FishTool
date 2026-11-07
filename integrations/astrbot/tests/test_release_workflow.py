"""守护：release workflow 里每一次调用 publish_branch.py 都显式传入推送令牌。

publish_branch.py 在临时仓库里推送，拿不到 actions/checkout 写在检出仓库里的认证头。
astrbot-v0.1.1 首次发布就因为孤儿分支那一步没传令牌而失败：
``fatal: could not read Username for 'https://github.com'``。
"""
from __future__ import annotations

import fnmatch
import re

from conftest import REPO_ROOT

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "astrbot-release.yml"


def _publish_steps() -> list[str]:
    text = WORKFLOW.read_text("utf-8")
    steps = re.split(r"\n\s*- name: ", text)
    return [s for s in steps if "publish_branch.py" in s]


def test_every_publish_step_passes_a_token():
    steps = _publish_steps()
    assert len(steps) == 2, "expected publish steps for the plugin repository and the orphan branch"
    for step in steps:
        name = step.splitlines()[0]
        match = re.search(r"--token-env\s+(\w+)", step)
        assert match, f"{name}: publish_branch.py must be called with --token-env"
        var = match.group(1)
        assert re.search(rf"^\s+{var}:\s*\$\{{\{{\s*secrets\.\w+\s*\}}\}}", step, re.M), (
            f"{name}: env {var} must come from a secret"
        )


def test_adapter_guard_skipped_only_for_mainline_prs():
    """主线 PR 跳过 test_main_untouched；适配分支（名含 astrbot）的 PR 和 push/tag 照常守护。"""
    text = WORKFLOW.read_text("utf-8")
    step = next(s for s in re.split(r"\n\s*- name: ", text) if s.startswith("Adapter tests"))
    match = re.search(r"^\s+FISHTOOL_SKIP_UNTOUCHED:\s*(.+)$", step, re.M)
    assert match, "Adapter tests 步骤需要按事件设置 FISHTOOL_SKIP_UNTOUCHED"
    assert match.group(1).strip() == (
        "${{ (github.event_name == 'pull_request' && !contains(github.head_ref, 'astrbot')) && '1' || '' }}"
    )


def test_plugin_repository_is_published_before_orphan_branch():
    """插件市场源优先：孤儿分支失败时，插件仓库已经发布成功。"""
    names = [s.splitlines()[0] for s in _publish_steps()]
    assert names[0].startswith("Publish to plugin repository"), names
    assert names[1].startswith('Publish to orphan branch "astrbot"'), names


MANIFEST = REPO_ROOT / "integrations" / "astrbot" / "release_tools" / "engine_manifest.txt"


def _manifest_includes() -> list[str]:
    lines = MANIFEST.read_text("utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith(("#", "!"))]


def _event_paths(event: str) -> list[str]:
    """取 on.<event>.paths 里的模式列表（纯文本解析，不依赖 PyYAML）。"""
    text = WORKFLOW.read_text("utf-8")
    block = re.search(rf"(?ms)^  {event}:\n((?:    .*\n|\n)+)", text)
    assert block, f"workflow 缺少 on.{event} 段"
    paths = re.search(r"(?ms)^    paths:\n((?:      - .*\n)+)", block.group(1))
    assert paths, f"on.{event} 缺少 paths 过滤"
    return re.findall(r'"([^"]+)"', paths.group(1))


def _covers(pattern: str, entry: str) -> bool:
    if entry.endswith("/"):
        return pattern == f"{entry}**"
    return pattern == entry or fnmatch.fnmatch(entry, pattern)


def test_workflow_paths_cover_engine_manifest():
    """插件包内含 engine_manifest.txt 打进 engine/ 的文件，改动它们也要触发本 workflow（#127）。"""
    includes = _manifest_includes()
    assert includes, "engine_manifest.txt 不应该为空"
    for event in ("push", "pull_request"):
        patterns = _event_paths(event)
        for entry in includes:
            assert any(_covers(p, entry) for p in patterns), f"on.{event}.paths 未覆盖 {entry}"


def test_release_job_gate_requires_tag_or_dispatch():
    """发布门不变：只有 astrbot-v* tag 或手动 workflow_dispatch 才会走到 release。"""
    text = WORKFLOW.read_text("utf-8")
    assert (
        "if: startsWith(github.ref, 'refs/tags/astrbot-v') || (github.event_name == 'workflow_dispatch')" in text
    )
