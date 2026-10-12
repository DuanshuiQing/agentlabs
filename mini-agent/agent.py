"""阶段 0 · 第 4 / 6 / 7 步：循环、两个出口、以及把每次运行落成轨迹。

第 3 步的代码里，"请求 A → 执行工具" 和 "请求 B" 是**同一段代码的两份拷贝**。
第 4 步把拷贝删掉、套上 while，于是它能一直走到模型不再请求工具为止。

循环体一共四件事，**出口在循环体内部**：

    ① resp = call_model(messages, TOOLS)     发请求
    ② if not msg.tool_calls: return ...      ★ 出口 1：唯一的正常出口 ★
    ③ messages.append(assistant_message)     回填 assistant 消息
    ④ messages.append(tool 消息)             回填工具结果
       （然后什么都不写，自然回到 ① —— 这就是"再走一轮"）

两个出口（**这不是进程退出码**，两种情况程序都正常返回、退出码都是 0：

    出口 1（正常）：模型不再请求工具，给出最终答案
    出口 2（保护）：步数用完，优雅退出并说明原因

⚠️ `while True` 不带上限等于把额度交给模型决定，所以上限必须有。

第 6 步做了两件事：

  1. **上限可配置**（`--max-steps`），于是"出口 2"能被**故意触发**来验收 ——
     一个从没被走到过的分支，等于没有测试过。
  2. **`run()` 返回 `Result` 而不是裸字符串**。原来两个出口都返回 `str`，
     调用方**分不出**手里的是"模型真的答了"还是"我们伪造的一句失败占位符"。
     成败必须用字段表达 —— 这是第 5 步那条原则（异常类名不进给模型的文本）
     的第二次应用：**给人看的文本 / 给程序看的字段，分开**。

顺手补的一处：出口 1 原来在 `append(assistant_message)` **之前** return，
所以最终答案从来没进过 `messages`（T3 实测：第 3 轮只发出 5 条，末条是 `tool`）。
现在补上了，`messages` 才是一份完整轨迹。

第 7 步：**每次运行落一份 `logs/run-<时间戳>.jsonl`**（PLAN §4.1 第 6 条）。
落盘逻辑全在 `runlog.py`，这里只负责在正确的位置调它。两种信息必须进文件，
而它们**都不在 `messages` 里**：

  · `usage`   —— 三个 token 数。没有它，"第 5 步那段错误文本改短了多少"
                 永远只是推测；有了它才是实测。
  · `is_error` / `error_kind` / `error_type` —— 第 5 步特意把异常类名从给模型的
                 文本里摘走了，说不许丢，要记在轨迹里。它现在挂在
                 `Observation`（`str` 的子类）上，模型看不见，复盘看得见。

运行：
    mini-agent\\.venv\\Scripts\\python.exe mini-agent\\agent.py
    mini-agent\\.venv\\Scripts\\python.exe mini-agent\\agent.py T3
    mini-agent\\.venv\\Scripts\\python.exe mini-agent\\agent.py T1 --max-steps 1   # 故意撞出口 2
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from typing import Any

# Windows 上 stdout 被重定向时 Python 会退回 GBK，上游按 UTF-8 解码会乱码
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from llm import MODEL, call_model
from runlog import RunLog
from tools import TOOLS, Observation, dispatch

MAX_STEPS = 10


def usage_of(resp: Any) -> dict | None:
    """把 SDK 的 usage 摊成 dict。

    用 `model_dump()` 而不是手挑三个字段：不同的兼容服务会多返回自己的字段
    （比如缓存命中数），摊开全记下来，将来想算什么都还有料。
    但累加时只看那三个标准字段 —— 不认识的字段不能参与求和。
    """
    usage = getattr(resp, "usage", None)
    if usage is None:
        return None
    dump = getattr(usage, "model_dump", None)
    if dump is None:
        return None
    return dump(exclude_none=True)


@dataclass(frozen=True)
class Result:
    """一次运行的结局。

    为什么不用裸字符串：两个出口都返回 `str` 的话，调用方无法分辨
    "模型真的答了" 和 "我们伪造的失败占位符" —— 失败绝不能长得像成功。
    阶段 1 的评测脚本要靠 `ok` 统计通过率，靠字符串前缀判断是不可接受的。
    """

    answer: str  # 给人看的文本
    ok: bool  # 给程序看的成败
    steps: int  # 实际用了几轮
    reason: str  # 出口 1 = 接口给的 finish_reason（'stop' / 'length' …）
    #              出口 2 = 'max_steps'


def assistant_message(msg: object) -> dict:
    """把模型的回复转成可以放回 messages 的 dict。

    漏了它，模型就"不记得"自己请求过调用工具，于是会重复请求同一个工具 ——
    死循环的起点（PLAN §5 避坑第 8 条）。

    ⚠️ `msg.tool_calls` 在**没有**工具调用时是 `None`，不是 `[]`。
    第 6 步之前这个函数只被"带 tool_calls"的分支调用，所以这个区别看不出来；
    出口 1 开始把最终回复也补进 messages 之后，它当场炸了：
        TypeError: 'NoneType' object is not iterable
    没有工具调用时**不要写 `tool_calls` 这个键** —— 服务端发来的原始消息里就没有它。
    """
    out: dict = {"role": "assistant", "content": msg.content}
    if msg.tool_calls:
        out["tool_calls"] = [
            {
                "id": c.id,
                "type": c.type,
                "function": {
                    "name": c.function.name,
                    "arguments": c.function.arguments,
                },
            }
            for c in msg.tool_calls
        ]
    return out


def execute(call: object) -> str:
    """把一次工具调用变成一段观察文本。任何问题都不抛出来。

    职责分工：JSON 字符串 -> dict 的解析留在 **agent 层**，
    因为那是"模型输出的线上格式"，属于协议；tools.py 不该知道协议。

    返回值仍然是 `str`，失败时是 `Observation`（`str` 的子类）——
    所以这里也要按第 7 步的规矩带上 error_kind / error_type，
    不能因为"这个错误是 agent 层发现的"就漏掉结构化信息。
    """
    raw = call.function.arguments
    try:
        args = json.loads(raw)
    except json.JSONDecodeError as exc:
        return Observation.failure(
            f"ERROR: 工具参数不是合法 JSON（{exc}）；原始文本：{raw[:200]!r}",
            "bad_arguments",
            "JSONDecodeError",
        )
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


def run(task: str, max_steps: int = MAX_STEPS) -> Result:
    """跑完整个循环，返回 Result，并把整条轨迹落进 `logs/run-<时间戳>.jsonl`。

    这里**允许**在参数是明显编程错误时 raise（例如 max_steps < 1）。
    AGENTS.md 那条"绝不抛异常"的硬规矩管的是 `dispatch` —— 工具的执行边界，
    因为只有模型有能力读着错误文本改策略。`run()` 是我们自己的外层边界，
    把调用方的低级错误静默吞掉，只会让它更晚暴露。

    用 `with RunLog(...)` 包住：中途崩了，`__exit__` 也会补一条 `role='result'`
    的崩溃行。"没有 trace 的实验等于没做" —— 而失败恰恰是最需要 trace 的时候。
    """
    if max_steps < 1:
        raise ValueError(f"max_steps 至少要 1，收到 {max_steps}")

    with RunLog(task=task, model=MODEL, max_steps=max_steps) as log:
        print(f"轨迹：{log.path}")
        messages: list[dict] = [{"role": "user", "content": task}]
        log.message(step=0, role="user", content=task)

        for step in range(1, max_steps + 1):
            print(f"\n[第 {step} 轮] 发请求，messages 共 {len(messages)} 条")
            t0 = time.perf_counter()
            resp = call_model(messages, TOOLS)
            model_s = time.perf_counter() - t0

            msg = resp.choices[0].message
            finish = resp.choices[0].finish_reason
            usage = usage_of(resp)
            log.note_usage(usage)
            spent = f"  tokens={usage['total_tokens']}" if usage else ""
            print(
                f"          <- finish_reason={finish!r}  "
                f"content={msg.content!r}  tool_calls={len(msg.tool_calls or [])} 个"
                f"  {model_s:.2f}s{spent}"
            )

            # 助手消息先落盘，再判断出口 —— 否则出口 1 那一轮的回复不会进文件
            log.message(
                step=step,
                role="assistant",
                content=msg.content,
                finish_reason=finish,
                tool_calls=[
                    {"id": c.id, "name": c.function.name, "arguments": c.function.arguments}
                    for c in (msg.tool_calls or [])
                ],
                usage=usage,
                elapsed_s=round(model_s, 3),
            )

            # ---- 出口 1：模型不再请求工具 —— 它答完了 ----
            if not msg.tool_calls:
                # 先把它补进 messages。少了这一步，最终答案就只活在返回值里，
                # 轨迹永远缺最后一句。
                messages.append(assistant_message(msg))
                show(messages, f"第 {step} 轮后（答案已进轨迹）")
                result = Result(
                    answer=msg.content or "",
                    # ok 同时要求"接口说它正常停了"和"确实拿到了非空文本"：
                    # finish_reason='length' 说明被 max_tokens 截断，答案不完整。
                    ok=bool(msg.content) and finish == "stop",
                    steps=step,
                    reason=finish or "stop",
                )
                log.result(
                    step=step, content=result.answer, ok=result.ok, reason=result.reason
                )
                return result

            # ---- 回填 assistant 消息（含它的 content 和 tool_calls）----
            messages.append(assistant_message(msg))

            # ---- 回填工具结果：每个 tool_call 对应一条 tool 消息 ----
            for call in msg.tool_calls:
                t1 = time.perf_counter()
                observation = execute(call)
                tool_s = time.perf_counter() - t1
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": observation}
                )
                print(f"          执行 {call.function.name}(...) -> {observation[:60]!r}")
                # 这里就是第 5 步欠下的那笔账：类名不进给模型的文本，
                # 但必须进轨迹。Observation 让这两件事同时成立。
                failed = isinstance(observation, Observation)
                log.message(
                    step=step,
                    role="tool",
                    content=observation,
                    tool_call_id=call.id,
                    tool_name=call.function.name,
                    arguments=call.function.arguments,
                    is_error=failed,
                    error_kind=observation.error_kind if failed else None,
                    error_type=observation.error_type if failed else None,
                    elapsed_s=round(tool_s, 3),
                )

            show(messages, f"第 {step} 轮回填后")

        # ---- 出口 2：步数用完 ----
        print(f"\n[停止] 已达步数上限 {max_steps}，未能得到最终答案。")
        print(f"       注意 messages 末条是 {messages[-1]['role']!r}：模型拿到了一次观察，")
        print("       却再没机会开口。那次工具调用的钱花了，结果被丢掉。")
        result = Result(
            answer=f"[未完成] 达到步数上限 {max_steps}，模型仍在请求工具调用。",
            ok=False,
            steps=max_steps,
            reason="max_steps",
        )
        log.result(step=max_steps, content=result.answer, ok=result.ok, reason=result.reason)
        return result


TASKS = {
    # T1：单工具、单步 —— 验证循环能正常收口
    "T1": "读取 data/handbook.txt，回答：项目约定的日志文件名是什么？",
    # T2：多步 —— 必须先用 list_dir 侦察，再从多个候选里挑对文件（PLAN §4.4）
    "T2": "在 data/ 目录里找出记录了本季度 API 预算的那个文件，告诉我金额。",
    # T3：目标文件**故意不存在** —— 验证失败被如实传下去，而不是被编造掩盖
    "T3": "读取 data/handbook-v2.txt 并总结它的内容。",
}

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="阶段 0 agent loop 验收脚本")
    parser.add_argument("task", nargs="?", default="T1", choices=sorted(TASKS))
    parser.add_argument(
        "--max-steps",
        type=int,
        default=MAX_STEPS,
        help="最多允许几轮（默认 %(default)s）",
    )
    cli = parser.parse_args()
    if cli.max_steps < 1:  # 命令行错误就用 argparse 报，别甩一坨 traceback 给人看
        parser.error("--max-steps 至少要 1")

    print(f"[{cli.task}] 任务：{TASKS[cli.task]}")
    print(f"        max_steps={cli.max_steps}")
    result = run(TASKS[cli.task], max_steps=cli.max_steps)

    print("\n===== 结果（四个字段，不是一句话）=====")
    print(f"  ok     = {result.ok}")
    print(f"  steps  = {result.steps}")
    print(f"  reason = {result.reason!r}")
    print(f"  answer = {result.answer}")
