"""结构化跑测试套件：给**判定器**用，输出一行 JSON 到 stdout。

为什么不直接解析 `python -m unittest -v` 的文本：
  · 长测试 id 在终端里会换行，`... ok` 可能落到下一行（探针里就见过）；
  · FAIL / ERROR 块与用例清单混在一起，正则要写得非常小心；
  · 一个模块 import 失败时，文本里只有一条 "ERROR: test_x (unittest.loader...)"，
    与"用例本身失败"很难区分 —— 而这两件事要分开记（判 `env-broken`）。

所以用 `unittest` 的编程接口自己收：每条用例归到 passed/failed/errored/skipped 之一。
**这个脚本不进沙箱**（agent 看不到它，也不会用它跑测试）；agent 用的是
`python -m unittest discover` 那条朴素命令，判定器才用这份结构化结果。

用法：

    python eval/runner.py --root <仓库副本>
    {"started": true, "passed": [...], "failed": [...], "errored": [...], "skipped": [...]}
"""

from __future__ import annotations

import argparse
import json
import sys
import unittest
from pathlib import Path


class Collector(unittest.TextTestResult):
    """把结果按类别收进 list，不做任何文本渲染。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.passed: list[str] = []
        self.failed_ids: list[str] = []
        self.errored: list[str] = []
        self.skipped: list[str] = []

    def addSuccess(self, test) -> None:
        super().addSuccess(test)
        self.passed.append(test.id())

    def addFailure(self, test, err) -> None:
        super().addFailure(test, err)
        self.failed_ids.append(test.id())

    def addError(self, test, err) -> None:
        super().addError(test, err)
        # setUpClass 失败时 test 是 _ErrorHolder，id() 形如 "setUpClass (module.Class)"
        self.errored.append(test.id())

    def addSkip(self, test, reason) -> None:
        super().addSkip(test, reason)
        self.skipped.append(test.id())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--pattern", default="test*.py")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    try:
        loader = unittest.TestLoader()
        suite = loader.discover(str(root / "tests"), pattern=args.pattern, top_level_dir=str(root))
    except Exception as exc:  # noqa: BLE001 —— 套件都装不起来，如实报告
        print(json.dumps({"started": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 0

    stream = open("nul", "w", encoding="utf-8")  # Windows 的空设备；不把 verbosity 输出写到 stdout
    try:
        runner = unittest.TextTestRunner(stream=stream, verbosity=0, resultclass=Collector)
        result = runner.run(suite)
    finally:
        stream.close()

    payload = {
        "started": True,
        "passed": result.passed,
        "failed": result.failed_ids,
        "errored": result.errored,
        "skipped": result.skipped,
        "tests": result.passed + result.failed_ids + result.errored + result.skipped,
    }
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
