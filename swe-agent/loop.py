"""四段循环：assemble → call → act → stop_gate。

    ① assemble   组装这一轮要发出去的 messages（M3 在这里插计划块 / 压缩摘要）
    ② call       发一次请求
    ③ act        回填 assistant 消息；逐个执行工具调用、回填 tool 消息
    ④ stop_gate  五个闸：测试全绿 / 步数 / token 预算 / 无进展 / 墙钟

**出口在循环体内部**，而且每一个出口都产出 `Result` 而不是一句话 ——
两个出口都返回裸字符串的话，调用方分不出"真的答完了"和"我们伪造的占位符"。

本轮的 `ok` 只表示"模型正常收尾**且**测试退出码为 0"，是 agent 的**自校验**；
最终裁量权在 `eval/judge.py`（还原 tests/ 后重跑）。两者故意分开：
自校验是给模型看的即时反馈，判定是给实验看的证据。
"""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tools
from llm import MODEL, call_model
from runlog import RunLog

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

MAX_STEPS = 30
BUDGET_TOKENS = 60_000
WALL_SECONDS = 480.0
NO_PROGRESS_LIMIT = 3  # 连续几轮"没写文件、也没让测试变好"就停

SYSTEM_PROMPT = """你是一名软件工程师 agent，在仓库的**副本**里工作：仓库根目录就是你的工作目录。

工作方式：
1. 先弄清问题。用 search 找相关代码或报错文本，用 outline 看文件结构，用 read_file 读具体行（带行号）。
2. 定位到根因之后再动手。改已有文件一律用 apply_edit：old 必须与文件内容逐字符一致，且只能匹配一处。
3. 每改一处立刻用 run_tests 验证；迭代时先跑单个用例（target=模块.类.方法）省时间，最后跑全量。
4. 测试全绿之前不要收尾。收尾时用一段话说清：根因是什么、改了哪个文件的哪几行、测试结果如何。

约束：
- tests/ 是项目约定（判据），不能修改；要让测试通过，就改实现。
- 不要猜文件名或文件名里的内容：用 list_dir / search 确认后再读。
- 一次只改一处，改完立刻验证。不要一口气改五个地方再跑一次测试。
- 如果查证后发现这个 bug 根本不存在、或无法复现，就如实报告，不要为了"交差"乱改代码。
- 不要向用户提问。遇到不确定，先把相关代码读清楚再决定。"""


@dataclass
class Features:
    """三个能力模块的开关（M3 才真正接上）。`label` 会进轨迹，用来区分消融臂。"""

    plan: bool = False
    reflect: bool = False
    compact: bool = False
    label: str = "A0"


@dataclass
class Result:
    answer: str
    ok: bool
    steps: int
    reason: str
    trace: str
    tokens: int = 0
    tests_exit: int | None = None
    tests_bad: int | None = None
    tool_calls: int = 0
    errors: list[str] = field(default_factory=list)


def assistant_message(msg: Any) -> dict:
    """把模型回复转成能放回 messages 的 dict。

    ⚠️ 没有工具调用时 `msg.tool_calls` 是 `None` 不是 `[]`；
    而且**不要**往消息里塞服务端没给的 `tool_calls` 键 —— 某些兼容服务会因此 400。
    """
    out: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
    if msg.tool_calls:
        out["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.function.name, "arguments": call.function.arguments},
            }
            for call in msg.tool_calls
        ]
    return out


def parse_args(raw: str | None) -> tuple[dict | None, str]:
    """解析工具参数。返回 (参数, 错误文本)；坏 JSON 也不许抛。"""
    text = (raw or "").strip() or "{}"
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"ERROR: 工具参数不是合法 JSON（{exc.msg}，位置 {exc.pos}）"
    if not isinstance(value, dict):
        return None, "ERROR: 工具参数必须是 JSON 对象"
    return value, ""


def bad_count_of(output: str) -> int | None:
    """从 `run_tests` 的文本里读出"还没通过的用例数"；判断不了就返回 None。"""
    if "全部通过" in output:
        return 0
    match = re.search(r"未通过 (\d+) 条", output)
    if match:
        return int(match.group(1))
    return None


def _verify() -> tuple[int, int | None]:
    """收尾自校验：在 agent 停下之后再跑一次全量测试，拿环境给的答案。

    注意解析的是 **unittest 的原始输出**（不是 `run_tests` 那段给模型看的格式化文本）：
    全绿时原始输出里没有"全部通过"这四个字，只有 `OK` 与退出码 0。
    """
    try:
        code, out = tools.run_suite_raw()
    except Exception:  # noqa: BLE001 —— 自校验失败不许把整个运行炸掉
        return -1, None
    if code == 0:
        return 0, 0
    failures = len(re.findall(r"^(?:FAIL|ERROR): ", out, re.M))
    # 一条都没数到，多半是套件根本没起来（导入失败）——这跟"改错了"是两回事
    return code, (failures or None)


def run(
    task: str,
    *,
    repo: str | Path,
    max_steps: int = MAX_STEPS,
    budget_tokens: int = BUDGET_TOKENS,
    wall_seconds: float = WALL_SECONDS,
    features: Features | None = None,
    verbose: bool = True,
    task_id: str = "",
) -> Result:
    if max_steps < 1:
        raise ValueError(f"max_steps 至少要 1，收到 {max_steps}")

    features = features or Features()
    root = tools.configure(repo)
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": task},
    ]

    with RunLog(
        task_id=task_id,
        task=task,
        model=MODEL,
        arm=features.label,
        max_steps=max_steps,
        budget_tokens=budget_tokens,
        sandbox=str(root),
    ) as log:
        log.message(step=0, role="system", content=SYSTEM_PROMPT)
        log.message(step=0, role="user", content=task)
        if verbose:
            print(f"轨迹：{log.path}")
            print(f"沙箱：{root}")

        started = time.perf_counter()
        spent = 0
        tool_calls_total = 0
        errors: list[str] = []
        last_bad: int | None = None
        idle = 0
        # 见过的调用签名。"同一个调用再来一次"不算进展；读过新文件才算。
        seen_calls: set[str] = set()
        _READ_TOOLS = {"read_file", "search", "outline", "list_dir"}

        for step in range(1, max_steps + 1):
            # ---- 闸 4 / 5：墙钟与预算（发请求之前就检查，省一次钱）----
            if time.perf_counter() - started > wall_seconds:
                return _finish(log, messages, step, "wall_clock", spent, features, verbose,
                               errors, tool_calls_total)
            if spent >= budget_tokens:
                return _finish(log, messages, step, "budget", spent, features, verbose,
                               errors, tool_calls_total)

            t0 = time.perf_counter()
            resp = call_model(messages, tools.TOOLS)
            model_s = time.perf_counter() - t0
            msg = resp.choices[0].message
            finish = resp.choices[0].finish_reason
            usage = _usage(resp)
            log.note_usage(usage)
            spent += (usage or {}).get("total_tokens", 0)
            if verbose:
                calls = len(msg.tool_calls or [])
                print(f"\n[第 {step} 轮] finish={finish!r} tool_calls={calls} "
                      f"tokens={spent} {model_s:.1f}s")

            log.message(
                step=step,
                role="assistant",
                content=msg.content or "",
                finish_reason=finish,
                tool_calls=[
                    {"id": c.id, "name": c.function.name, "arguments": c.function.arguments}
                    for c in (msg.tool_calls or [])
                ],
                usage=usage,
                elapsed_s=round(model_s, 3),
            )

            # ---- 出口 1：模型不再请求工具 ----
            if not msg.tool_calls:
                messages.append(assistant_message(msg))
                code, bad = _verify()
                ok = finish == "stop" and bool((msg.content or "").strip()) and code == 0
                answer = (msg.content or "").strip()
                log.event(step=step, event="verify", exit_code=code, bad_tests=bad)
                log.result(
                    step=step,
                    ok=ok,
                    reason=finish or "stop",
                    content=answer,
                    tests_exit=code,
                    tests_bad=bad,
                    tokens=spent,
                )
                if verbose:
                    print(f"        [自校验] 测试退出码 {code}，未通过 {bad}")
                return Result(
                    answer=answer + ("" if bad in (None, 0) else f"\n\n[自校验] 测试仍未通过 {bad} 条"),
                    ok=ok,
                    steps=step,
                    reason=finish or "stop",
                    trace=log.path.name,
                    tokens=spent,
                    tests_exit=code,
                    tests_bad=bad,
                    tool_calls=tool_calls_total,
                    errors=errors,
                )

            messages.append(assistant_message(msg))

            # ---- act：逐个执行工具 ----
            wrote = False
            fresh_info = False
            step_bad: int | None = None
            for call in msg.tool_calls:
                name = call.function.name
                args, problem = parse_args(call.function.arguments)
                signature = f"{name}:{json.dumps(args or {}, ensure_ascii=False, sort_keys=True)}"
                t1 = time.perf_counter()
                if problem:
                    observation = tools.Observation(problem, error_kind="bad-json")
                else:
                    observation = tools.dispatch(name, args)
                tool_s = time.perf_counter() - t1
                tool_calls_total += 1
                messages.append({"role": "tool", "tool_call_id": call.id, "content": observation})
                if observation.is_error:
                    errors.append(f"{name}:{observation.error_kind}")
                if name in ("apply_edit", "write_file") and not observation.is_error:
                    wrote = True
                # "拿到新信息"也是一条正当的进展路径 —— 少了它，查证阶段会被误判成空转
                # （S11 实测：读到第 3 个文件就被 no_progress 闸掐掉了）
                if name in _READ_TOOLS and not observation.is_error and signature not in seen_calls:
                    fresh_info = True
                seen_calls.add(signature)
                if name == "run_tests":
                    step_bad = bad_count_of(str(observation))

                log.message(
                    step=step,
                    role="tool",
                    content=str(observation),
                    tool_call_id=call.id,
                    tool_name=name,
                    arguments=call.function.arguments,
                    is_error=bool(observation.is_error),
                    error_kind=observation.error_kind,
                    error_type=observation.error_type,
                    elapsed_s=round(tool_s, 3),
                )
                if verbose:
                    flag = f"!{observation.error_kind}" if observation.is_error else ""
                    print(f"        {name}({_short(args)}) -> {str(observation)[:60]!r}{flag}")

            # ---- 闸 4：无进展检测 ----
            improved = step_bad is not None and (last_bad is None or step_bad < last_bad)
            if step_bad is not None:
                last_bad = step_bad if last_bad is None else min(last_bad, step_bad)
            if wrote or fresh_info or improved:
                idle = 0
            else:
                idle += 1
                log.event(step=step, event="idle", count=idle, wrote=wrote, bad=step_bad)
            if idle >= NO_PROGRESS_LIMIT:
                log.event(step=step, event="stop", reason="no_progress", idle=idle)
                return _finish(log, messages, step, "no_progress", spent, features, verbose,
                               errors, tool_calls_total)

        # ---- 出口 2：步数用完 ----
        return _finish(log, messages, max_steps, "max_steps", spent, features, verbose,
                       errors, tool_calls_total)


def _finish(
    log: RunLog,
    messages: list[dict],
    step: int,
    reason: str,
    spent: int,
    features: Features,
    verbose: bool,
    errors: list[str],
    tool_calls: int,
) -> Result:
    """非正常收尾：不伪造"答案"，把原因如实写进 Result 与轨迹。"""
    code, bad = _verify()
    text = {
        "max_steps": f"达到步数上限 {step}，仍未完成。",
        "budget": f"token 预算用完（已用 {spent}），仍未完成。",
        "wall_clock": "墙钟上限到了，仍未完成。",
        "no_progress": f"连续 {NO_PROGRESS_LIMIT} 轮既没有写文件、也没有让测试变好，已停止。",
    }.get(reason, reason)
    if verbose:
        print(f"\n[停止] {text}")
    log.event(step=step, event="stop", reason=reason, tokens=spent)
    log.result(
        step=step,
        ok=False,
        reason=reason,
        content=text,
        tests_exit=code,
        tests_bad=bad,
        tokens=spent,
    )
    return Result(
        answer=f"[未完成] {text}",
        ok=False,
        steps=step,
        reason=reason,
        trace=log.path.name,
        tokens=spent,
        tests_exit=code,
        tests_bad=bad,
        tool_calls=tool_calls,
        errors=errors,
    )


def _usage(resp: Any) -> dict | None:
    usage = getattr(resp, "usage", None)
    dump = getattr(usage, "model_dump", None)
    return dump(exclude_none=True) if dump else None


def _short(args: dict) -> str:
    text = ", ".join(f"{k}={str(v)[:28]!r}" for k, v in args.items())
    return text[:70]
