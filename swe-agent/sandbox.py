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
    """agent 造成的改动（tracked 文件）。未跟踪的新文件不在里面 —— 用 `full_patch`。"""
    return _git(repo, "diff").stdout


def full_patch(repo: Path) -> str:
    """**完整** patch：已跟踪文件的修改 + 新建文件。

    为什么不能只用 `git diff`：agent 用 `write_file` 新建的文件是 untracked，
    `git diff` 一个字都看不到；而 `--rejudge` 正是靠重放 patch 才不用重跑 API。
    `git add -A` 只动索引、不动工作区内容，所以取完 patch 再还原 tests/ 即可。
    """
    _git(repo, "add", "-A")
    return _git(repo, "diff", "--cached").stdout


def restore_all(repo: Path) -> None:
    """把工作区与索引都退回基线 commit（重判前用）。"""
    _git(repo, "reset", "--hard", "HEAD")


def apply_patch(repo: Path, patch_file: Path) -> tuple[bool, str]:
    """把归档的 patch 重放到一字不差的干净副本上；返回 (是否成功, 说明)。"""
    if not patch_file.exists():
        return False, f"patch 不存在: {patch_file.name}"
    proc = _git(repo, "apply", "--whitespace=nowarn", str(patch_file))
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout).strip()[:200]
    return True, "ok"


def status_of(repo: Path) -> list[str]:
    """`git status --porcelain` 的每一行；用来发现"改了 tests/"或"新建了文件"。"""
    return [line for line in _git(repo, "status", "--porcelain").stdout.splitlines() if line.strip()]


def tests_tampered(repo: Path) -> bool:
    return any(line[3:].replace("\\", "/").startswith("tests/") for line in status_of(repo))


def restore_tests(repo: Path) -> None:
    """判定前把 tests/ 还原成**基线 commit** 里的版本 —— 工具层拦一遍，这里再拦一遍。

    ⚠️ 必须是 `git checkout HEAD -- tests`，不能是 `git checkout -- tests`：
    后者从**索引**还原，而取完整 patch 时执行过 `git add -A`，篡改过的测试已经进了索引。
    写成后者的话，"还原"会原样还回篡改版 —— 判据失效且没有任何报错（判定器自检抓到过）。
    """
    _git(repo, "checkout", "HEAD", "--", "tests")
