"""最小 agent loop —— 把「决策 → 调工具 → 观察 → 再决策」跑给你看。

这个文件里唯一被伪造的是"模型"（FakeModel）；循环本身、工具调用协议、
消息历史、错误回传、终止条件全都是真的。
把 FakeModel.decide() 换成一次真实的 LLM 调用，你就得到一个真 agent，
结构一行都不用改 —— 任何 agent 框架剥到最里面，都是这个循环。

运行（用 DSH 内置 Python 3.12，见工作区 README 的工具链口径）：
    & "C:\\Users\\Lenovo\\.dsh\\dsh-runtimes\\dsh-primary-runtime\\dependencies\\python\\python.exe" labs/01-agent-loop/agent_loop.py
"""

from __future__ import annotations

import ast
import json
import operator
import sys
from pathlib import Path
from typing import Any, Callable

# Windows 上当 stdout 不是终端（被重定向或被别的程序捕获）时，Python 会退回
# 系统区域编码（中文环境是 GBK），而上游往往按 UTF-8 解码 → 中文全变乱码。
# 这一行强制 UTF-8 输出。终端里若仍乱码，先执行 chcp 65001。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[2]  # → D:\Agent_test

# ---------------------------------------------------------------------------
# 1. 工具层：真的会被执行的函数
# ---------------------------------------------------------------------------

_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARYOPS = {ast.USub: operator.neg, ast.UAdd: operator.pos}


def _eval_node(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        return _BINOPS[type(node.op)](_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARYOPS:
        return _UNARYOPS[type(node.op)](_eval_node(node.operand))
    raise ValueError(f"不支持的表达式片段：{ast.dump(node)}")


def calculator(expression: str) -> str:
    """算四则运算。用 ast 白名单，绝不用 eval()。"""
    return str(_eval_node(ast.parse(expression, mode="eval")))


def read_file(path: str) -> str:
    """读取工作区内的文本文件，最多返回前 500 字符。"""
    target = (ROOT / path).resolve()
    if target != ROOT and ROOT not in target.parents:
        raise PermissionError(f"越界：{path} 不在工作区内")
    return target.read_text(encoding="utf-8")[:500]


TOOLS: dict[str, Callable[..., str]] = {
    "calculator": calculator,
    "read_file": read_file,
}

# 真实 agent 里，这段 schema 会作为「工具定义」一起发给模型。
# 模型能不能正确用你的工具，八成取决于 description 写得清不清楚。
TOOL_SCHEMAS = [
    {
        "name": "calculator",
        "description": "计算一个算术表达式，支持 + - * / % **",
        "parameters": {"expression": "字符串，例如 '12*(3+4)'"},
    },
    {
        "name": "read_file",
        "description": "读取工作区内的文本文件，返回前 500 字符",
        "parameters": {"path": "相对工作区根的路径，例如 'README.md'"},
    },
]


def execute_tool(name: str, arguments: dict[str, Any]) -> str:
    """执行工具。任何异常都变成一段文本观察结果交回模型 —— 这是关键设计：
    模型不会因为工具报错而崩溃，它把错误当成新的输入继续决策。"""
    func = TOOLS.get(name)
    if func is None:
        return f"ERROR: 没有名为 {name!r} 的工具"
    try:
        return func(**arguments)
    except Exception as exc:  # noqa: BLE001 —— 故意兜住一切，转成观察结果
        return f"ERROR: {type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# 2. 模型层：★ 这里被伪造了 ★
# ---------------------------------------------------------------------------


class FakeModel:
    """规则化的假模型。输出格式与真模型一致：要么给工具调用，要么给最终答案。

    ★ 想接真模型，只需要改 decide() 这一个方法：
      把 history 发给 LLM，解析它返回的 tool_call / final 即可。★
    """

    def decide(self, history: list[dict[str, Any]]) -> dict[str, Any]:
        tool_results = [m for m in history if m["role"] == "tool"]
        n = len(tool_results)

        if n == 0:
            return {
                "thought": "先算 12*(3+4)。",
                "tool": "calculator",
                "arguments": {"expression": "12*(3+4)"},
            }
        if n == 1:
            return {
                "thought": "顺手试一下除零，我想看工具报错是怎么传回来的。",
                "tool": "calculator",
                "arguments": {"expression": "84/0"},
            }
        if n == 2:
            return {
                "thought": "错误被当成观察结果回来了，循环没崩。接着读 README 第一行。",
                "tool": "read_file",
                "arguments": {"path": "README.md"},
            }
        first_line = tool_results[-1]["content"].strip().splitlines()[0]
        product = tool_results[0]["content"]
        return {
            "thought": "两个信息都到手，可以给最终答案了。",
            "final": f"{product} 是 12*(3+4) 的结果；README 第一行是：{first_line}",
        }


# ---------------------------------------------------------------------------
# 3. 循环层：agent 的骨架
# ---------------------------------------------------------------------------


def run(task: str, max_steps: int = 8, verbose: bool = True) -> str:
    history: list[dict[str, Any]] = [{"role": "user", "content": task}]
    model = FakeModel()

    if verbose:
        print("=" * 72)
        print(f"任务：{task}")
        print("=" * 72)

    for step in range(1, max_steps + 1):
        decision = model.decide(history)  # ← 换成真 LLM 的唯一位置
        thought = decision.get("thought", "")

        if "final" in decision:
            if verbose:
                print(f"\n[第 {step} 步] 思考：{thought}")
                print(f"          → 最终答案：{decision['final']}")
                print(
                    f"\n循环结束：第 {step} 步产出最终答案，"
                    f"共 {step - 1} 次工具调用。"
                )
            return decision["final"]

        name = decision["tool"]
        args = decision.get("arguments", {})

        if verbose:
            print(f"\n[第 {step} 步] 思考：{thought}")
            print(f"          → 调用 {name}({json.dumps(args, ensure_ascii=False)})")

        observation = execute_tool(name, args)

        if verbose:
            label = "观察（错误回传）" if observation.startswith("ERROR") else "观察"
            print(f"          ← {label}：{observation.splitlines()[0][:100]}")

        history.append(
            {
                "role": "assistant",
                "content": thought,
                "tool_call": {"name": name, "arguments": args},
            }
        )
        history.append({"role": "tool", "name": name, "content": observation})

    print(f"\n达到 max_steps={max_steps} 仍未产出最终答案 —— 这就是死循环保护。")
    return ""


if __name__ == "__main__":
    print("发给模型的工具定义：")
    for schema in TOOL_SCHEMAS:
        print(f"  · {schema['name']}: {schema['description']}")
    print()

    run("算出 12*(3+4)，再把工作区 README 的第一行告诉我。")
