"""阶段 0 · 第 7 步：把一次运行落成可复盘的 JSONL 轨迹。

为什么单独一个模块，而不是塞进 agent.py：
    agent.py 的职责是"循环长什么样"，这里只管"怎么落盘"。
    混在一起，第 8 步再加工具时循环会被日志代码淹没 ——
    而这一阶段的全部价值就是看清循环。

（顺带一个命名决定：这个文件**不叫** `trace.py`。标准库里已经有一个 `trace`
模块，用同名文件去遮蔽它，将来某天会变成一个极难查的 bug。）

格式不是我定的，是 `data/handbook.txt` 第 5、6、7 条定的：

    5. 文件名 run-<时间戳>.jsonl，放在 logs/ 目录下
    6. 一行一条 JSON，每条**至少**含 step、role、content 三个字段
    7. 同一条任务重复运行，日志不得互相覆盖

第 6 条那个"至少"是留了口子：三个必备字段之外随便加。于是：

    开场行   role='run'      额外带 run_id / model / max_steps / started_at
    user 行  role='user'     就是 messages[0] 的原样副本，回放靠它
    助手行   role='assistant' 额外带 finish_reason / tool_calls / usage / elapsed_s
    工具行   role='tool'      额外带 tool_name / arguments / is_error /
                              error_kind / error_type / elapsed_s
    收尾行   role='result'    额外带 ok / reason / usage_total / elapsed_s

`run` 和 `result` 这两个角色名不在模型的角色表（system/user/assistant/tool）里，
所以回放时**永远不会和真实上下文消息混淆** —— 但也因此，每条都得凑齐那三个字段。

⚠️ 换行必须是 LF（handbook 第 4 条）。Windows 上 `open()` 默认把 `\n` 翻成 `\r\n`，
   所以要显式 `newline="\n"`。本工作区为 CRLF 已经踩过一次 git 警告的坑。
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

# Windows 上 stdout 被重定向时 Python 会退回 GBK，上游按 UTF-8 解码会乱码
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent
LOGS_DIR = HERE / "logs"
_USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")


def _new_log_path(now: datetime | None = None) -> Path:
    """`run-<时间戳>.jsonl`；同名就依次加 `-2`、`-3`……（handbook 第 7 条）。

    时间戳只精确到秒，两次运行落在同一秒是完全可能的 —— 一条 `while path.exists()`
    就够了，不需要毫秒，也不需要随机数。
    """
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    path = LOGS_DIR / f"run-{stamp}.jsonl"
    n = 2
    while path.exists():
        path = LOGS_DIR / f"run-{stamp}-{n}.jsonl"
        n += 1
    return path


class RunLog:
    """一次运行的轨迹文件。用 `with` 包住，异常也保证留下痕迹。"""

    def __init__(self, task: str, model: str, max_steps: int) -> None:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        self.path = _new_log_path()
        self.run_id = self.path.stem
        self.wrote_result = False
        self.usage_total = dict.fromkeys(_USAGE_KEYS, 0)
        self._t0 = time.perf_counter()
        self._fh: TextIO = self.path.open("w", encoding="utf-8", newline="\n")
        self.message(
            step=0,
            role="run",
            content=task,
            run_id=self.run_id,
            model=model,
            max_steps=max_steps,
            started_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )

    # -- 写 ---------------------------------------------------------------

    def message(self, *, step: int, role: str, content: Any, **extra: Any) -> None:
        """写一行。

        `step` / `role` / `content` 三个字段**永远都在**（handbook 第 6 条）。
        content 允许是 None —— assistant 请求工具的那一轮，content 常常是空的，
        但那个键必须在，否则回放程序得为缺失字段写特例。
        """
        record = {"step": step, "role": role, "content": content}
        record.update(extra)
        self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        # 每行都 flush：进程被 Ctrl-C 掐掉时，已经走过的步骤必须还在文件里。
        # 轨迹的价值在失败时最大 —— 攒在缓冲区里的 trace 等于没写。
        self._fh.flush()

    def note_usage(self, usage: dict | None) -> None:
        """累加一次模型调用的 token 消耗。"""
        if not usage:
            return
        for key in _USAGE_KEYS:
            self.usage_total[key] += usage.get(key, 0)

    def result(self, *, step: int, content: str, ok: bool, reason: str, **extra: Any) -> None:
        """收尾行。

        usage_total 和 elapsed_s 由这里自己填 —— "这次运行一共花了多少"
        本来就该由"一次运行"这个对象来回答，不该让调用方到处累加。
        """
        self.message(
            step=step,
            role="result",
            content=content,
            ok=ok,
            reason=reason,
            usage_total=dict(self.usage_total),
            elapsed_s=round(time.perf_counter() - self._t0, 3),
            **extra,
        )
        self.wrote_result = True

    # -- 生命周期 ---------------------------------------------------------

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> "RunLog":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is not None and not self.wrote_result:
            # 崩了也必须留下一行。只有这一行能告诉你"跑到第几步炸的、炸在什么上"。
            self.result(
                step=-1,
                content=f"[崩溃] {exc_type.__name__}: {exc}",
                ok=False,
                reason="exception",
                error_type=exc_type.__name__,
            )
        self.close()
        return False  # 绝不吞异常：日志只负责记录，不负责掩盖


if __name__ == "__main__":
    # 自检：不花一分钱，只验文件名不覆盖、字段齐全、换行是 LF。
    #
    # ⚠️ 不用 tempfile.TemporaryDirectory()：本机（DSH 沙箱下）它给出的
    # C:\...\Temp\dsh-*\ 目录**能建但不能写文件**，PermissionError。
    # 临时目录一律放在工作区内 —— README 里那条"tempfile 会掉进工作区"的同类坑。
    import shutil

    tmp = LOGS_DIR / "_selftest"
    tmp.mkdir(parents=True, exist_ok=True)
    LOGS_DIR = tmp  # 模块级赋值：_new_log_path 读的就是这个全局名

    print("=== 同一秒内连开三个日志 ===")
    logs = [RunLog("任务", "fake-model", 3) for _ in range(3)]
    try:
        for log in logs:
            print(f"  {log.path.name}")
        assert len({log.path for log in logs}) == 3, "文件名撞了，handbook 第 7 条被破坏"

        print("\n=== 字段齐全 + 换行是 LF ===")
        logs[0].message(step=1, role="assistant", content=None, elapsed_s=0.5)
        logs[0].note_usage({"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12})
        logs[0].result(step=1, content="答案", ok=True, reason="stop")

        raw = logs[0].path.read_bytes()
        print(f"  行数 = {len(raw.splitlines())}，含 CR = {b'\\r' in raw}")
        assert b"\r" not in raw, "出现了 CR，handbook 第 4 条的 LF 约定被破坏"
        for line in raw.decode("utf-8").splitlines():
            obj = json.loads(line)
            missing = {"step", "role", "content"} - obj.keys()
            assert not missing, f"缺字段 {missing}：{line}"
            print(f"  {line[:96]}")
        print("\n  每一行都解析成功，且三个必备字段都在。")
    finally:
        for log in logs:
            log.close()
        shutil.rmtree(tmp, ignore_errors=True)
