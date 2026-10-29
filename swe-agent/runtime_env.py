"""子进程运行时环境 —— 工具层（`run_tests`）和判定层（`eval/suite.py`）共用同一份。

必须共用的理由：**agent 跑测试看到的世界，必须和判定器看到的一样**。
两份 env 一旦分叉，"agent 说他跑绿了、判定说还红着"这种最难查的偏差就会冒出来。

两个成分：

1. **白名单**：不把 `OPENAI_API_KEY` 之类递进 agent 能影响的进程。
   agent 会写代码，而这些代码随后由 `run_tests` 执行 —— 环境变量是最便宜的越权通道。
2. **`PYTHONIOENCODING=utf-8`**：M0 探针量出来的。子进程 stdout 是管道时
   Python 退回系统 locale（本机 GBK），父进程按 UTF-8 解码就是乱码。

⚠️ **故意不设 `PYTHONUTF8=1`**。它会把 `open()` 的默认编码也变成 UTF-8，
于是"忘了写 `encoding='utf-8'`"这类真 bug 在 Windows 上**再也不会被触发** ——
而 S04 那条任务测的正是它。这个开关省下的是麻烦，丢掉的是一条判据。
"""

from __future__ import annotations

import os

_KEEP = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
)


def child_env() -> dict[str, str]:
    env = {k: os.environ[k] for k in _KEEP if k in os.environ}
    env["PYTHONIOENCODING"] = "utf-8"
    return env
