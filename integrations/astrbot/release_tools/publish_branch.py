"""把构建好的插件树发布为一个 Git 分支（只追加提交，不强推）。

两个发布目标，CI 都会写：

- FishTool 仓库的孤儿分支 ``astrbot``：``…/FishTool/tree/astrbot`` 安装用。
- 独立插件仓库（``FORK-is-coding/astrbot_plugin_FishTool``）的默认分支：AstrBot 插件市场
  只解析仓库默认分支，FishTool 默认分支是 main，所以上架用独立仓库。

用法::

    # 本仓库孤儿分支
    python integrations/astrbot/release_tools/publish_branch.py dist/plugin --branch astrbot \
        --message "release astrbot-v0.1.0 from <sha>"

    # 独立插件仓库（令牌经环境变量传入，不出现在命令行与日志里）
    PLUGIN_REPO_TOKEN=*** python integrations/astrbot/release_tools/publish_branch.py dist/plugin \
        --repo-url https://github.com/FORK-is-coding/astrbot_plugin_FishTool.git --branch main \
        --token-env PLUGIN_REPO_TOKEN --message "release v0.1.1 from FishTool@<sha>"

做法（不碰当前工作区）：

1. 在临时目录建一个独立 git 仓库；远端已有该分支则取回其最新提交作为父提交，否则建孤儿提交。
2. 工作区内容整体替换为构建产物。
3. 无变化则跳过；否则提交，``--no-push`` 以外推送 ``HEAD:refs/heads/<branch>``（非强推，远端被
   别人改动过会被拒绝）。
"""
from __future__ import annotations

import argparse
import base64
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

logger = logging.getLogger("fishtool.publish")
REPO_ROOT = Path(__file__).resolve().parents[3]
#: 调用方仓库没有配置提交身份时使用（GitHub Actions 的检出仓库默认没有）。
BOT_IDENTITY = {
    "user.name": "github-actions[bot]",
    "user.email": "41898282+github-actions[bot]@users.noreply.github.com",
}


class PublishError(RuntimeError):
    """git 操作失败。消息中不含令牌。"""


def _redact(text: str, secrets_: list[str]) -> str:
    for secret in secrets_:
        if secret:
            text = text.replace(secret, "***")
    return text


def git(*args: str, cwd: Path, check: bool = True, secrets_: list[str] | None = None,
        extra_config: list[str] | None = None) -> subprocess.CompletedProcess:
    cmd = ["git"]
    for item in extra_config or []:
        cmd += ["-c", item]
    cmd += list(args)
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        hidden = list(secrets_ or [])
        shown = " ".join(_redact(a, hidden) for a in args)
        raise PublishError(f"git {shown} failed: {_redact(proc.stderr.strip(), hidden)}")
    return proc


def auth_config(token: str) -> tuple[list[str], list[str]]:
    """GitHub HTTPS 认证：一次性 http.extraheader，令牌不写进 remote URL 或 .git/config。

    Returns:
        (git -c 配置列表, 需要在错误信息中隐藏的字符串)
    """
    if not token:
        return [], []
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return [f"http.https://github.com/.extraheader=AUTHORIZATION: basic {basic}"], [token, basic]


def _identity(repo: Path) -> list[str]:
    """沿用调用方仓库的提交身份；没有配置时用 github-actions[bot]（CI 的检出仓库默认没有身份）。"""
    out = []
    for key in ("user.name", "user.email"):
        value = git("config", key, cwd=repo, check=False).stdout.strip() or BOT_IDENTITY[key]
        out.append(f"{key}={value}")
    return out


def _replace_tree(src: Path, wt: Path) -> None:
    for child in wt.iterdir():
        if child.name == ".git":
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    for child in src.iterdir():
        dst = wt / child.name
        if child.is_dir():
            shutil.copytree(child, dst)
        else:
            shutil.copy2(child, dst)


def publish(
    src: Path,
    branch: str,
    message: str,
    *,
    remote_url: str | None = None,
    token: str = "",
    push: bool = True,
    repo: Path = REPO_ROOT,
) -> str:
    """发布插件树并返回新提交 sha（无变化时返回远端现有 sha）。

    Args:
        src: 构建好的插件树（含 metadata.yaml）。
        branch: 目标分支。
        message: 提交说明。
        remote_url: 目标仓库地址；None 表示调用方仓库的 origin。
        token: 推送令牌（只用于 HTTPS 认证头）。
        push: False 时只在临时仓库提交，不推送（演练）。
        repo: 调用方仓库（取 origin 地址与提交身份）。
    """
    src = src.resolve()
    if not (src / "metadata.yaml").is_file():
        raise FileNotFoundError(f"{src} is not a plugin tree (metadata.yaml missing)")
    url = remote_url or git("remote", "get-url", "origin", cwd=repo).stdout.strip()
    auth, hidden = auth_config(token)
    ident = _identity(repo)

    tmp = Path(tempfile.mkdtemp(prefix="astrbot_publish_"))
    wt = tmp / "wt"
    wt.mkdir()
    try:
        git("init", "-q", cwd=wt)
        git("remote", "add", "target", url, cwd=wt)
        has_remote = git(
            "ls-remote", "--exit-code", "--heads", "target", branch,
            cwd=wt, check=False, extra_config=auth,
        ).returncode == 0
        if has_remote:
            git("fetch", "-q", "--depth", "1", "target", f"refs/heads/{branch}",
                cwd=wt, secrets_=hidden, extra_config=auth)
            git("checkout", "-q", "-B", branch, "FETCH_HEAD", cwd=wt)
        else:
            git("checkout", "-q", "--orphan", branch, cwd=wt)

        _replace_tree(src, wt)
        git("add", "-A", "-f", ".", cwd=wt)
        if has_remote and git("diff", "--cached", "--quiet", cwd=wt, check=False).returncode == 0:
            sha = git("rev-parse", "HEAD", cwd=wt).stdout.strip()
            logger.info("no changes to publish on %s (%s)", branch, sha[:8])
            return sha
        git("commit", "-q", "-m", message, cwd=wt, extra_config=ident)
        sha = git("rev-parse", "HEAD", cwd=wt).stdout.strip()
        if push:
            git("push", "-q", "target", f"HEAD:refs/heads/{branch}",
                cwd=wt, secrets_=hidden, extra_config=auth)
            logger.info("pushed %s to %s", sha[:8], branch)
        else:
            logger.info("committed %s locally, not pushed (dry run)", sha[:8])
        return sha
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="publish plugin tree to a git branch")
    parser.add_argument("src")
    parser.add_argument("--branch", default="astrbot")
    parser.add_argument("--repo-url", default=None, help="目标仓库；默认本仓库 origin")
    parser.add_argument("--token-env", default="", help="存放推送令牌的环境变量名（不在命令行传令牌）")
    parser.add_argument("--message", required=True)
    parser.add_argument("--no-push", action="store_true")
    args = parser.parse_args(argv)
    token = os.environ.get(args.token_env, "") if args.token_env else ""
    if args.token_env and not token:
        parser.error(f"environment variable {args.token_env} is empty")
    try:
        publish(Path(args.src), args.branch, args.message,
                remote_url=args.repo_url, token=token, push=not args.no_push)
    except PublishError as exc:
        logger.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
