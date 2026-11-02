"""把构建好的插件树写入孤儿发布分支 ``astrbot``（只追加提交，不强推）。

用法::

    python integrations/astrbot/release_tools/publish_branch.py dist/plugin --branch astrbot \
        --message "release astrbot-v0.1.0 from <sha>" [--no-push]

做法（不碰当前工作区）：

1. ``git worktree add --detach <tmp>``；远端已有分支则检出它，否则 ``checkout --orphan``。
2. 清空工作区（保留 .git），复制构建产物。
3. ``git add -A && git commit``；``--no-push`` 以外执行 ``git push origin <branch>``。
4. ``git worktree remove``。
"""
from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

logger = logging.getLogger("fishtool.publish")
REPO_ROOT = Path(__file__).resolve().parents[3]


def git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc


def publish(src: Path, branch: str, message: str, remote: str = "origin", push: bool = True, repo: Path = REPO_ROOT) -> str:
    """发布并返回新提交 sha。"""
    src = src.resolve()
    if not (src / "metadata.yaml").is_file():
        raise FileNotFoundError(f"{src} is not a plugin tree (metadata.yaml missing)")
    tmp = Path(tempfile.mkdtemp(prefix="astrbot_publish_"))
    wt = tmp / "wt"
    git("worktree", "add", "--detach", str(wt), cwd=repo)
    try:
        has_remote = git("ls-remote", "--exit-code", "--heads", remote, branch, cwd=repo, check=False).returncode == 0
        if has_remote:
            git("fetch", remote, f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}", cwd=wt)
            git("checkout", "-B", branch, f"{remote}/{branch}", cwd=wt)
        else:
            git("checkout", "--orphan", branch, cwd=wt)
        for child in wt.iterdir():
            if child.name == ".git":
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        git("rm", "-r", "-q", "--cached", "--ignore-unmatch", ".", cwd=wt, check=False)
        for child in src.iterdir():
            dst = wt / child.name
            if child.is_dir():
                shutil.copytree(child, dst)
            else:
                shutil.copy2(child, dst)
        git("add", "-A", "-f", ".", cwd=wt)
        if git("diff", "--cached", "--quiet", cwd=wt, check=False).returncode == 0 and has_remote:
            logger.info("no changes to publish")
            return git("rev-parse", "HEAD", cwd=wt).stdout.strip()
        git("commit", "-q", "-m", message, cwd=wt)
        sha = git("rev-parse", "HEAD", cwd=wt).stdout.strip()
        if push:
            git("push", remote, f"HEAD:refs/heads/{branch}", cwd=wt)
            logger.info("pushed %s to %s/%s", sha[:8], remote, branch)
        else:
            git("branch", "-f", branch, sha, cwd=repo, check=False)
            logger.info("committed %s locally (not pushed)", sha[:8])
        return sha
    finally:
        git("worktree", "remove", "--force", str(wt), cwd=repo, check=False)
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="publish plugin tree to orphan branch")
    parser.add_argument("src")
    parser.add_argument("--branch", default="astrbot")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--message", required=True)
    parser.add_argument("--no-push", action="store_true")
    args = parser.parse_args(argv)
    publish(Path(args.src), args.branch, args.message, args.remote, push=not args.no_push)
    return 0


if __name__ == "__main__":
    sys.exit(main())
