"""把一次运行落成可复盘的 JSONL 轨迹。

**来源**：复制自 `mini-agent/runlog.py`（同一个作者、同一个工作区的阶段 0 产物），
本文件相对上游的差异全部列在下面 —— 目的不是"重造"，而是阶段 1 的评测要能查
计划/反思/压缩这三类事件，而上游把 `LOGS_DIR` 钉死在 mini-agent 下。

与上游的差异：
  1. `LOGS_DIR` 指向 `swe-agent/logs/`；行里多一个 `event` 维度（plan/reflect/compact/verify）。
  2. 开跑行多记 `task_id` / `arm` / `budget_tokens` / `sandbox`。
  3. `result` 行多记 `tokens` / `tests_exit` / `files_changed` / `patch`。
  4. `note_usage` 同时累计四个字段（prompt/completion/total + 缓存命中，若有）。

格式沿用上游（也是 `mini-agent/data/handbook.txt` 第 5–7 条定的）：

    文件名  logs/run-<YYYYMMDD-HHMMSS>.jsonl，一行一条 JSON，同一任务重复运行不覆盖
    必备字段 step / role / content；其余随便加
    换行一律 LF（Windows 上 open() 默认写 CRLF，必须显式 newline="\n"）
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent
LOGS_DIR = HERE / "logs"
PATCH_DIR = LOGS_DIR / "patches"

# "什么算一份轨迹"的唯一数据源：命名与"不给模型看"的拦截规则都从这里派生。
TRACE_GLOB = "run-*.jsonl"
_USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")


def new_trace_path() -> Path:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = LOGS_DIR / f"run-{stamp}.jsonl"
    serial = 1
    while path.exists():  # 同一秒内跑第二次也不能覆盖
        path = LOGS_DIR / f"run-{stamp}-{serial}.jsonl"
        serial += 1
    return path


class RunLog:
    """with 语句包住整个循环：中途崩了也要留下一条 `reason='exception'` 的收尾行。"""

    def __init__(
        self,
        *,
        task_id: str = "",
        task: str = "",
        model: str = "",
        arm: str = "A0",
        max_steps: int = 0,
        budget_tokens: int = 0,
        sandbox: str = "",
    ) -> None:
        self.path = new_trace_path()
        self.started = time.perf_counter()
        self.usage_total = dict.fromkeys(_USAGE_KEYS, 0)
        self.extra_usage: dict[str, int] = {}
        self.step = 0
        self._fh: TextIO | None = None
        self._header = {
            "run_id": self.path.stem,
            "task_id": task_id,
            "task": task,
            "model": model,
            "arm": arm,
            "max_steps": max_steps,
            "budget_tokens": budget_tokens,
            "sandbox": sandbox,
            "started_at": datetime.now().isoformat(timespec="seconds"),
        }

    # -- 生命周期 ---------------------------------------------------------
    def __enter__(self) -> "RunLog":
        self._fh = self.path.open("w", encoding="utf-8", newline="\n")
        self._write({"role": "run", "content": "", "step": 0, **self._header})
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None and self._fh is not None:
            self.result(ok=False, reason="exception", content=f"{exc_type.__name__}: {exc}")
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        return False  # 异常继续往上抛，别吞

    # -- 写 -----------------------------------------------------------------
    def _write(self, row: dict[str, Any]) -> None:
        if self._fh is None:
            return
        self._fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._fh.flush()  # 崩了也要能看到崩在哪一步

    def message(self, *, step: int, role: str, content: Any, **extra: Any) -> None:
        self.step = max(self.step, step)
        row = {"step": step, "role": role, "content": content}
        row.update(extra)
        self._write(row)

    def event(self, *, step: int, event: str, **fields: Any) -> None:
        """非对话事件：计划、反思、压缩、终止检查。"""
        self._write({"step": step, "role": "event", "event": event, "content": "", **fields})

    def note_usage(self, usage: dict | None) -> None:
        if not usage:
            return
        for key, value in usage.items():
            if not isinstance(value, int):
                continue
            if key in self.usage_total:
                self.usage_total[key] += value
            else:
                self.extra_usage[key] = self.extra_usage.get(key, 0) + value

    def result(self, *, ok: bool, reason: str, content: str = "", **extra: Any) -> None:
        row = {
            "step": self.step,
            "role": "result",
            "content": content,
            "ok": ok,
            "reason": reason,
            "usage_total": dict(self.usage_total),
            "extra_usage": dict(self.extra_usage),
            "elapsed_s": round(time.perf_counter() - self.started, 2),
        }
        row.update(extra)
        self._write(row)

    def save_patch(self, diff_text: str) -> str:
        """把 agent 的改动存成独立文件 —— 判定与 `--rejudge` 都靠它，不含任何 API 调用。"""
        return save_patch(self.path.stem, diff_text)


def save_patch(trace_stem: str, diff_text: str) -> str:
    PATCH_DIR.mkdir(parents=True, exist_ok=True)
    path = PATCH_DIR / f"{trace_stem}.diff"
    path.write_text(diff_text, encoding="utf-8", newline="\n")
    return path.name


if __name__ == "__main__":
    # 自检：写一份日志、读回来、断言必备字段与 LF
    with RunLog(task_id="selftest", task="自检", model="none", max_steps=1) as log:
        log.message(step=0, role="user", content="你好")
        log.message(step=1, role="assistant", content="答复", usage={"total_tokens": 7})
        log.note_usage({"total_tokens": 7, "prompt_tokens": 5, "prompt_cache_hit_tokens": 2})
        log.event(step=1, event="reflect", trigger="same-failure")
        log.result(ok=True, reason="stop", content="完成")
    raw = log.path.read_bytes()
    rows = [json.loads(x) for x in raw.decode("utf-8").splitlines()]
    assert b"\r\n" not in raw, "日志里出现了 CRLF"
    assert rows[0]["role"] == "run" and rows[0]["task_id"] == "selftest"
    assert rows[-1]["role"] == "result" and rows[-1]["ok"] is True
    assert log.usage_total["total_tokens"] == 7
    assert log.extra_usage["prompt_cache_hit_tokens"] == 2
    assert any(r.get("event") == "reflect" for r in rows)
    print(f"自检通过：{log.path.name}，{len(rows)} 行，{len(raw)} 字节")
    log.path.unlink()
