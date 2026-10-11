"""阶段 0 · 第 4 步：把单轮驱动换成 while 循环 —— 这就是一个完整的 agent 了。

第 3 步的代码里，"请求 A → 执行工具" 和 "请求 B" 是**同一段代码的两份拷贝**。
这一步把拷贝删掉、套上 while，于是它能一直走到模型不再请求工具为止。

循环体一共四件事，**出口在循环体内部**：

    ① resp = call_model(messages, TOOLS)     发请求
    ② if not msg.tool_calls: return ...      ★ 唯一的正常出口 ★
    ③ messages.append(assistant_message)     回填 assistant 消息
    ④ messages.append(tool 消息)             回填工具结果
       （然后什么都不写，自然回到 ① —— 这就是"再走一轮"）

⚠️ 关于 max_steps：`while True` 不带上限等于把额度交给模型决定。
所以这一步就把上限带上。它有两个出口，第二个是安全网：

    出口 1（正常）：模型不再请求工具，给出最终答案
    出口 2（保护）：步数用完，优雅退出并说明原因

第 6 步会把上限做成可配置，并故意设成 2 来观察出口 2。

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

MAX_STEPS = 10


def assistant_message(msg: object) -> dict:
    """把模型的回复转成可以放回 messages 的 dict。

    漏了它，模型就"不记得"自己请求过调用工具，于是会重复请求同一个工具 ——
    死循环的起点（PLAN §7 避坑第 8 条）。
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


def show(messages: list[dict], title: str) -> None:
    """打印 messages 的形状。"""
    print(f"\n--- {title}：messages 共 {len(messages)} 条 ---")
    for i, m in enumerate(messages):
        role = m["role"]
        if role == "tool":
            print(f"  [{i}] {role:<10} tool_call_id={m['tool_call_id']}  {m['content'][:52]!r}")
        elif role == "assistant" and m.get("tool_calls"):
            names = [c["function"]["name"] for c in m["tool_calls"]]
            print(f"  [{i}] {role:<10} tool_calls={names}  content={m['content']!r}")
        else:
            print(f"  [{i}] {role:<10} {m['content']!r}")


def run(task: str, max_steps: int = MAX_STEPS) -> str:
    """跑完整个循环，返回最终答案。"""
    messages: list[dict] = [{"role": "user", "content": task}]

    for step in range(1, max_steps + 1):
        print(f"\n[第 {step} 轮] 发请求，messages 共 {len(messages)} 条")
        resp = call_model(messages, TOOLS)
        msg = resp.choices[0].message
        print(
            f"          <- finish_reason={resp.choices[0].finish_reason!r}  "
            f"content={msg.content!r}  tool_calls={len(msg.tool_calls or [])} 个"
        )

        # ---- 出口 1：模型不再请求工具，这就是最终答案 ----
        if not msg.tool_calls:
            return msg.content or ""

        # ---- 回填 assistant 消息（含它的 content 和 tool_calls）----
        messages.append(assistant_message(msg))

        # ---- 回填工具结果：每个 tool_call 对应一条 tool 消息 ----
        for call in msg.tool_calls:
            observation = execute(call)
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": observation}
            )
            print(f"          执行 {call.function.name}(...) -> {observation[:60]!r}")

        show(messages, f"第 {step} 轮回填后")

    # ---- 出口 2：步数用完 ----
    print(f"\n[停止] 已达步数上限 {max_steps}，未能得到最终答案。")
    return f"[未完成] 达到步数上限 {max_steps}，模型仍在请求工具调用。"


TASKS = {
    # T1：单工具、单步 —— 验证循环能正常收口
    "T1": "读取 data/handbook.txt，回答：项目约定的日志文件名是什么？",
    # T3：目标文件**故意不存在** —— 验证失败被如实传下去，而不是被编造掩盖
    "T3": "读取 data/handbook-v2.txt 并总结它的内容。",
}

if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "T1"
    task = TASKS[which]
    print(f"[{which}] 任务：{task}")
    answer = run(task)
    print(f"\n===== 最终答案 =====\n{answer}")
