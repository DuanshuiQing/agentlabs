"""跑套件的公共层：子进程环境、结构化结果、把种子铺进沙箱。

两个消费者：`check_fixtures.py`（零成本的题库自检）和 `judge.py`（判定）。
两边必须用**同一个** `run_suite`，否则"自检说没问题、判定说有问题"这类
最难查的偏差就会出现。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
SWE = HERE.parent
RUNNER = HERE / "runner.py"

for _extra in (SWE,):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

# 子进程环境**只有一份**定义，和 agent 的 `run_tests` 共用（见 runtime_env 的注释）
from runtime_env import child_env  # noqa: E402

__all__ = ["Suite", "run_suite", "apply_seeds", "child_env", "RUNNER"]


@dataclass
class Suite:
    started: bool
    passed: set[str] = field(default_factory=set)
    failed: set[str] = field(default_factory=set)
    errored: set[str] = field(default_factory=set)
    skipped: set[str] = field(default_factory=set)
    error: str = ""

    @property
    def all_ids(self) -> set[str]:
        return self.passed | self.failed | self.errored | self.skipped

    @property
    def bad(self) -> set[str]:
        """真正"没通过"的：失败 + 出错。skip 不算坏（本套件里没有 skip）。"""
        return self.failed | self.errored

    def brief(self) -> str:
        if not self.started:
            return f"套件没能启动：{self.error[:160]}"
        return (
            f"共 {len(self.all_ids)} 条：通过 {len(self.passed)}"
            f" / 失败 {len(self.failed)} / 出错 {len(self.errored)}"
        )


def run_suite(repo: Path, timeout: float = 180.0) -> Suite:
    proc = subprocess.run(
        [sys.executable, str(RUNNER), "--root", str(repo)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=child_env(),
        cwd=str(repo),
    )
    line = (proc.stdout or "").strip().splitlines()[-1] if (proc.stdout or "").strip() else ""
    if not line.startswith("{"):
        return Suite(
            started=False,
            error=f"runner 没输出 JSON（退出码 {proc.returncode}）: "
            f"{(proc.stderr or proc.stdout or '')[-200:]!r}",
        )
    data = json.loads(line)
    if not data.get("started"):
        return Suite(started=False, error=str(data.get("error", "")))
    return Suite(
        started=True,
        passed=set(data.get("passed") or []),
        failed=set(data.get("failed") or []),
        errored=set(data.get("errored") or []),
        skipped=set(data.get("skipped") or []),
    )


def apply_seeds(repo: Path, files_dir: Path) -> list[str]:
    """把 `tasks/<id>/files/**` 覆盖到仓库副本上；返回被覆盖的相对路径。"""
    applied: list[str] = []
    if not files_dir.is_dir():
        return applied
    for src in sorted(files_dir.rglob("*")):
        if not src.is_file():
            continue
        rel = src.relative_to(files_dir)
        dst = repo / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        applied.append(rel.as_posix())
    return applied
