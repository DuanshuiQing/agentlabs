"""阶段 0 · 第 3 步：工具注册表 + dispatch —— 把「模型的请求」变成「一段文本观察」。

第 2 步我们只让模型"说出"它想调什么，没有执行。这一步补上执行。
（第 2 步那个只打印 tool_calls 的探针已被 agent.py 完整取代，故移除。）

三条铁律（第 3 步的全部内容）：

  1. **dispatch 一律返回字符串，绝不向外抛异常。**
     模型是靠读文本改变策略的；抛出异常等于把它掐断，它就没机会换条路。

  2. **校验必须发生在调用之前。**
     SDK 自己在 `Function.arguments` 的文档里警告过：
     "the model does not always generate valid JSON, and may hallucinate
      parameters not defined by your function schema. Validate the arguments
      in your code before calling your function."
     —— 所以"参数 JSON 解析失败""参数名不存在""缺必填参数"都不是理论风险。

  3. **schema 与校验规则只能有一个数据源。**
     下面用 ToolSpec 同时生成「给模型看的 schema」和「dispatch 用的校验」。
     如果手工维护两份，第 5、8 步再加工具时必然不一致 ——
     而不一致的后果是：模型按 schema 填参数，dispatch 却把它判为非法。

运行（纯本地，不发 API 请求）：
    mini-agent\\.venv\\Scripts\\python.exe mini-agent\\tools.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# Windows 上 stdout 被重定向时 Python 会退回 GBK，上游按 UTF-8 解码会乱码
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 工具路径的基准目录 = mini-agent 目录。
# 这样 PLAN §4.4 的任务文本（"读取 data/handbook.txt"）可以原样使用。
ROOT = Path(__file__).resolve().parent
MAX_CHARS = 2000


def _resolve(path: str) -> Path:
    """把工具收到的相对路径解析成绝对路径，并拒绝越出 mini-agent 目录。"""
    target = (ROOT / path).resolve()
    if target != ROOT and ROOT not in target.parents:
        raise PermissionError(f"路径越界：{path} 不在 mini-agent 目录内")
    return target


def read_file(path: str) -> str:
    """读取 mini-agent 目录内的一个文本文件。

    ⚠️ 这个函数是给 dispatch 调用的。模型看不见它，也执行不了它。
    """
    target = _resolve(path)
    text = target.read_text(encoding="utf-8")
    if len(text) > MAX_CHARS:
        return text[:MAX_CHARS] + f"\n...（已截断，原文件共 {len(text)} 字符）"
    return text


# ---------------------------------------------------------------------------
# 工具注册表：schema 与校验的唯一数据源
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    """一个工具的全部信息。"""

    name: str
    description: str
    params: dict[str, str]  # 参数名 -> 给模型看的说明
    required: tuple[str, ...]
    func: Callable[..., str]

    def schema(self) -> dict:
        """生成发给模型的 JSON Schema。不手工维护第二份。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        p: {"type": "string", "description": d}
                        for p, d in self.params.items()
                    },
                    "required": list(self.required),
                },
            },
        }

    def validate(self, args: object) -> str | None:
        """校验参数。返回错误说明字符串；None 表示通过。

        注意：现在所有参数都是字符串。将来若出现 int/bool 参数，
        这里需要按类型扩展 —— 那一步我会明确说明。
        """
        if not isinstance(args, dict):
            return f"参数必须是一个 JSON 对象，收到的是 {type(args).__name__}"

        missing = [k for k in self.required if k not in args]
        if missing:
            return f"缺少必填参数 {missing}；本工具需要 {list(self.required)}"

        unknown = [k for k in args if k not in self.params]
        if unknown:
            return f"出现 schema 里没有的参数 {unknown}；本工具只接受 {list(self.params)}"

        wrong = [k for k, v in args.items() if not isinstance(v, str)]
        if wrong:
            return f"参数 {wrong} 必须是字符串"

        return None


_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="read_file",
        # description 是模型选不选这个工具的唯一依据，必须和实现的真实边界一致。
        description=(
            "读取 mini-agent 目录内的一个文本文件，返回其内容（最多前 2000 字符）。"
            "当需要知道某个文件里写了什么时使用。"
            "path 必须是相对 mini-agent 目录的路径，例如 'requirements.txt'。"
        ),
        params={"path": "相对 mini-agent 目录的文件路径，例如 'requirements.txt'"},
        required=("path",),
        func=read_file,
    ),
)

_REGISTRY: dict[str, ToolSpec] = {spec.name: spec for spec in _TOOL_SPECS}

# 模型能看见的全部内容就是这个列表，它由上面的 ToolSpec 生成
TOOLS: list[dict] = [spec.schema() for spec in _TOOL_SPECS]


def dispatch(name: str, args: dict) -> str:
    """执行工具。必须返回字符串；任何异常都转成字符串返回，绝不向外抛。

    Args:
        name: 模型请求的工具名
        args: 已经解析好的参数字典。JSON 解析由 agent 层负责 ——
              tools 层不该知道模型输出的线上格式。
    """
    spec = _REGISTRY.get(name)
    if spec is None:
        return f"ERROR: 没有名为 {name!r} 的工具。可用工具：{sorted(_REGISTRY)}"

    problem = spec.validate(args)
    if problem is not None:
        return f"ERROR: 参数不合法 —— {problem}"

    try:
        result = spec.func(**args)
    except Exception as exc:  # noqa: BLE001 —— 故意兜住一切，转成文本回喂
        return f"ERROR: {type(exc).__name__}: {exc}"

    if not isinstance(result, str):
        return f"ERROR: 工具 {name!r} 返回了 {type(result).__name__}，dispatch 只能返回字符串"
    return result


# ---------------------------------------------------------------------------
# 失败路径自检 —— 第 3 步"测什么"的主要部分
# ---------------------------------------------------------------------------

_FAILURE_CASES: tuple[tuple[str, str, object], ...] = (
    ("工具名不存在（模型可能编）", "write_file", {"path": "requirements.txt"}),
    ("少了必填参数（模型可能漏）", "read_file", {}),
    (
        "多了 schema 没有的参数（模型可能编）",
        "read_file",
        {"path": "requirements.txt", "encoding": "utf-8"},
    ),
    ("参数类型不对（模型可能给数字）", "read_file", {"path": 123}),
    ("参数根本不是 JSON 对象", "read_file", "requirements.txt"),
    ("文件不存在（T3 要考的就是这个）", "read_file", {"path": "data/nope.txt"}),
    ("路径越界（安全边界）", "read_file", {"path": "../README.md"}),
)


def _self_test() -> None:
    print("=== dispatch 的失败路径自检（全部本地执行，不发 API 请求）===")
    for label, name, args in _FAILURE_CASES:
        out = dispatch(name, args)
        print(f"\n[{label}]")
        print(f"  dispatch({name!r}, {args!r})")
        print(f"  -> {out}")

    print("\n=== 正常路径对照 ===")
    out = dispatch("read_file", {"path": "requirements.txt"})
    print("  dispatch('read_file', {'path': 'requirements.txt'}) 前 60 字符：")
    print(f"  -> {out[:60]!r}")
    print("\n注意每一行都以 ERROR 开头且**没有抛出异常** —— 这就是 dispatch 存在的意义。")


if __name__ == "__main__":
    _self_test()
