"""阶段 0 · 第 2 步：定义工具（schema + 真实实现），让模型返回一次 tool_calls。

这一步最重要的认知，请先记住这一句：

    **模型不会执行任何东西。**

它只是输出一段文本，内容形如「我想调用 read_file，参数是 {"path": "..."}」。
模型看不见这个文件里的 `read_file()` 函数，也永远不会调用它 ——
那个函数是给**你的 dispatch** 调用的。

所以本步的演示故意在拿到 tool_calls 之后就停下，**不执行任何工具**。
执行是第 3 步的事。

运行：
    mini-agent\\.venv\\Scripts\\python.exe mini-agent\\tools.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Windows 上 stdout 被重定向时 Python 会退回 GBK，上游按 UTF-8 解码会乱码
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 工具路径的基准目录 = mini-agent 目录。
#
# 为什么现在就把它定死：因为 description 里必须写明「路径相对于哪里」，
# 否则模型会猜，一猜就错。定成 mini-agent 目录，是为了让 PLAN §4.4 里的
# 任务文本（"读取 data/handbook.txt"）可以**原样**使用，不需要加前缀。
ROOT = Path(__file__).resolve().parent
MAX_CHARS = 2000


def _resolve(path: str) -> Path:
    """把工具收到的相对路径解析成绝对路径，并拒绝越出工作区。"""
    target = (ROOT / path).resolve()
    if target != ROOT and ROOT not in target.parents:
        raise PermissionError(f"路径越界：{path} 不在工作区内")
    return target


def read_file(path: str) -> str:
    """读取 mini-agent 目录内的一个文本文件。

    ⚠️ 这个函数是给 agent 的 dispatch 调用的。模型看不见它，也执行不了它。
    模型能看见的，只有下面 TOOLS 里的那段 schema。
    """
    target = _resolve(path)
    text = target.read_text(encoding="utf-8")
    if len(text) > MAX_CHARS:
        return text[:MAX_CHARS] + f"\n...（已截断，原文件共 {len(text)} 字符）"
    return text


# ---------------------------------------------------------------------------
# 模型能看见的全部内容 —— 就是这个列表
#
# 注意它和上面的 read_file() 之间没有任何技术联系：
# 函数名只是**恰好**和 schema 里的 name 一致，靠的是你的 dispatch 去对应。
# 换句话说：模型看到的是一张「菜单」，不是一个「函数」。
# ---------------------------------------------------------------------------
TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            # description 是模型选不选这个工具的唯一依据。
            # 它必须回答三件事：做什么、什么时候用、参数怎么填。
            # ⚠️ 措辞必须和实现的真实边界完全一致：下面 _resolve() 只允许
            #    mini-agent 目录内的路径，所以这里不能含糊地说「工作区内」，
            #    否则模型会去试 ../xxx 这种路径，然后收到一个它看不懂的错误。
            "description": (
                "读取 mini-agent 目录内的一个文本文件，返回其内容（最多前 2000 字符）。"
                "当需要知道某个文件里写了什么时使用。"
                "path 必须是相对 mini-agent 目录的路径，"
                "例如 'requirements.txt' 或 'data/handbook.txt'。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "相对 mini-agent 目录的文件路径，例如 'requirements.txt'",
                    }
                },
                "required": ["path"],
            },
        },
    }
]


def _probe() -> None:
    """手工探针：发一次带工具定义的请求，看清 tool_calls 长什么样。"""
    from llm import call_model  # 同目录导入 —— llm.py 就在旁边

    task = "requirements.txt 里写了什么？"
    messages = [{"role": "user", "content": task}]

    print(f"任务           : {task}")
    print(f"发给模型的工具 : {[t['function']['name'] for t in TOOLS]}\n")

    resp = call_model(messages, TOOLS)
    msg = resp.choices[0].message

    print("=== 模型这次返回了什么 ===")
    print(f"finish_reason : {resp.choices[0].finish_reason!r}")
    print(f"content       : {msg.content!r}")
    print(f"tool_calls    : {len(msg.tool_calls or [])} 个")

    if not msg.tool_calls:
        print("\n模型没有请求调用工具。")
        print("这通常说明 description 写得不够清楚 —— 改 description 再试。")
        return

    for i, call in enumerate(msg.tool_calls, 1):
        raw_args = call.function.arguments
        print(f"\n--- 第 {i} 个工具调用 ---")
        print(f"  id（服务端生成，你不能自己编）: {call.id}")
        print(f"  type                          : {call.type!r}")
        print(f"  function.name                 : {call.function.name!r}")
        print(f"  function.arguments（是字符串）: {raw_args!r}")
        try:
            print(f"  解析成 dict 之后              : {json.loads(raw_args)}")
        except json.JSONDecodeError as exc:
            print(f"  解析失败（模型给了非法 JSON）  : {exc}")

    print("\n=== 到此为止 ===")
    print("我没有执行 read_file。模型只是输出了一段 JSON 文本，")
    print("真正去读文件的那行代码，第 3 步才写。")

    print(f"\n对照：如果你只看 content，你会看到 {msg.content!r}")
    print("      —— 这就是「不能把响应拍扁成字符串」的直接证据。")


if __name__ == "__main__":
    _probe()
