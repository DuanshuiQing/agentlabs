"""沙箱副本：每次运行从干净仓库复制一份，播种、提交基线、最后取 patch。

为什么"复制"而不是"直接在原件上改"：**跑一次就等于一次不可逆的修改**。
原件一旦被改，P2P 基线就没了，之后所有数字都不可比。
复制 + git 基线 commit 换来三件事：

  · 重置便宜（`git checkout -- . && git clean -fdx`）；
  · patch 可归档（`git diff`），判定与重判都不再需要 agent 重跑；
  · 篡改测试留痕（`tests_tampered` 由 `git status` 判出来）。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from fsutil import rmtree_force

HERE = Path(__file__).resolve().parent
SANDBOX_ROOT = HERE / "sandbox"
GIT_ID = ("-c", "user.name=swe-agent", "-c", "user.email=swe-agent@local")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *GIT_ID, "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def prepare(*, name: str, source: Path, seed_files: Path | None = None) -> Path:
    """建好沙箱副本并提交基线；返回副本根目录。"""
    dest = SANDBOX_ROOT / name
    rmtree_force(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, dest, ignore=shutil.ignore_patterns(".git", "__pycache__", ".tmp"))

    if seed_files is not None and seed_files.is_dir():
        for src in sorted(seed_files.rglob("*")):
            if src.is_file():
                target = dest / src.relative_to(seed_files)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, target)

    for args, what in ((("init", "-q"), "init"), (("add", "-A"), "add"),
                       (("commit", "-qm", "baseline"), "commit")):
        proc = _git(dest, *args)
        if proc.returncode != 0:
            raise RuntimeError(f"沙箱 git {what} 失败: {(proc.stderr or proc.stdout).strip()[:200]}")
    return dest


def diff_of(repo: Path) -> str:
    """agent 造成的改动（tracked 文件）。未跟踪的新文件不在里面 —— 单独看 `untracked`。"""
    return _git(repo, "diff").stdout


def status_of(repo: Path) -> list[str]:
    """`git status --porcelain` 的每一行；用来发现"改了 tests/"或"新建了文件"。"""
    return [line for line in _git(repo, "status", "--porcelain").stdout.splitlines() if line.strip()]


def tests_tampered(repo: Path) -> bool:
    return any(line[3:].replace("\\", "/").startswith("tests/") for line in status_of(repo))


def restore_tests(repo: Path) -> None:
    """判定前把 tests/ 还原成基线 —— 工具层拦一遍，这里再拦一遍（两层都要有）。"""
    _git(repo, "checkout", "--", "tests")
