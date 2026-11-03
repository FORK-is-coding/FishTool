"""守护：release workflow 里每一次调用 publish_branch.py 都显式传入推送令牌。

publish_branch.py 在临时仓库里推送，拿不到 actions/checkout 写在检出仓库里的认证头。
astrbot-v0.1.1 首次发布就因为孤儿分支那一步没传令牌而失败：
``fatal: could not read Username for 'https://github.com'``。
"""
from __future__ import annotations

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
