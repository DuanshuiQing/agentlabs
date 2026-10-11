"""阶段 0 · 第 3 步：走完一轮完整的工具闭环（**这里还没有 while 循环**）。

一次闭环要发两次请求：

    请求 A:  [user]                         -> 模型回 [assistant + tool_calls]
             （你执行工具，并把两条消息收进历史）
    请求 B:  [user, assistant, tool]        -> 模型回最终答案

**请求 B 能给出最终答案，全靠请求 A 的两条消息被正确收进历史。**
漏掉任何一条，请求 B 都会报 400（已实测）。

第 3 步只走一轮。第 4 步把 run() 包进 while，它就能一直走下去 ——
那时你会看到"单轮"到"无限轮"只差几行。

运行：
    mini-agent\\.venv\\Scripts\\python.exe mini-agent\\agent.py
"""

from __future__ import annotations

import json
import sys

# Windows 上 stdout 被重定向时 Python 会退回 GBK，上游按 UTF-8 解码会乱码
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from llm import call_model
from tools import TOOLS, dispatch


def assistant_message(msg: object) -> dict:
    """把模型的回复转成可以放回 messages 的 dict。

    这一步是最容易漏的地方：漏了它，模型就"不记得"自己请求过调用工具，
    于是会重复请求同一个工具 —— 死循环的起点（PLAN §7 避坑第 8 条）。
    """
    return {
        "role": "assistant",
        "content": msg.content,
        "tool_calls": [
            {
                "id": c.id,
                "type": c.type,
                "function": {
                    "name": c.function.name,
                    "arguments": c.function.arguments,
                },
            }
            for c in msg.tool_calls
        ],
    }


def execute(call: object) -> str:
    """把一次工具调用变成一段观察文本。任何问题都不抛出来。

    职责分工：JSON 字符串 -> dict 的解析留在 **agent 层**，
    因为那是"模型输出的线上格式"，属于协议；tools.py 不该知道协议。
    """
    raw = call.function.arguments
    try:
        args = json.loads(raw)
    except json.JSONDecodeError as exc:
        return f"ERROR: 工具参数不是合法 JSON（{exc}）；原始文本：{raw[:200]!r}"
    return dispatch(call.function.name, args)


def show(messages: list[dict], title: str = "") -> None:
    """打印 messages 的形状 —— 第 3 步最该盯住的东西。"""
    if title:
        print(f"\n--- {title} ---")
    print(f"  messages 现在有 {len(messages)} 条：")
    for i, m in enumerate(messages):
        role = m["role"]
        if role == "tool":
            print(f"    [{i}] {role:<10} tool_call_id={m['tool_call_id']}  {m['content'][:55]!r}")
        elif role == "assistant" and m.get("tool_calls"):
            names = [c["function"]["name"] for c in m["tool_calls"]]
            print(f"    [{i}] {role:<10} tool_calls={names}  content={m['content']!r}")
        else:
            print(f"    [{i}] {role:<10} {m['content']!r}")


def run(task: str) -> str:
    """第 3 步版本：只走一轮。第 4 步会加 while 循环。"""
    messages: list[dict] = [{"role": "user", "content": task}]
    show(messages, "起始状态")

    # ---------------- 请求 A ----------------
    print("\n[请求 A] 只带 user 消息")
    resp = call_model(messages, TOOLS)
    msg = resp.choices[0].message
    print(
        f"          -> finish_reason={resp.choices[0].finish_reason!r}  "
        f"content={msg.content!r}  tool_calls={len(msg.tool_calls or [])} 个"
    )

    if not msg.tool_calls:
        print("          模型直接给了答案，这轮不需要工具。")
        return msg.content or ""

    # 【消息 1】assistant 必须原样收进历史（含它的 content）
    messages.append(assistant_message(msg))

    # 【消息 2..n】每个 tool_call 对应一条 tool 消息
    for call in msg.tool_calls:
        observation = execute(call)
        messages.append(
            {"role": "tool", "tool_call_id": call.id, "content": observation}
        )
        print(f"          执行 {call.function.name}(...) -> {observation[:70]!r}")

    show(messages, "请求 A 之后（注意新增的两条）")

    # ---------------- 请求 B ----------------
    print("\n[请求 B] 带上 user + assistant + tool")
    resp2 = call_model(messages, TOOLS)
    msg2 = resp2.choices[0].message
    print(f"          -> finish_reason={resp2.choices[0].finish_reason!r}")

    if msg2.tool_calls:
        names = [c.function.name for c in msg2.tool_calls]
        print(f"          -> 模型还想再调 {names}")
        print("          第 3 步到此为止：单轮驱动处理不了第二次工具调用。")
        print("          这正是第 4 步 while 循环要解决的问题。")
        return "[未完成] 单轮驱动不够用"

    return msg2.content or ""


if __name__ == "__main__":
    task = "requirements.txt 里写了什么？"
    print(f"任务：{task}")
    answer = run(task)
    print(f"\n===== 最终答案 =====\n{answer}")
