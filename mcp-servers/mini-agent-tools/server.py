"""一个最小的 MCP server（stdio）：把本项目的只读能力暴露给任何 MCP 客户端。

## 为什么从 stdio 开始

规范给了两种标准传输（[Transports, 2025-06-18]）：**stdio** 与 **Streamable HTTP**。
stdio 的约束只有五条，全都可验证，所以它适合当第一次实现：

1. 客户端把 server 当**子进程**拉起；
2. server 从 `stdin` 读 JSON-RPC 消息、向 `stdout` 写消息；
3. 消息以**换行**分隔，**不得含内嵌换行**；
4. 可以往 `stderr` 写日志（客户端可转发或忽略）；
5. **`stdout` 上不许写任何不是合法 MCP 消息的东西**。

第 5 条是最容易踩的：任何一句 `print()` 都会污染协议流，客户端会直接解析失败。
所以本文件里 **stdout 只由 `send()` 一个函数写**，日志一律走 `stderr`。

## 三个工具，都不是玩具

| 工具 | 它做什么 | 为什么这样设计 |
|---|---|---|
| `read_project_file` | 读 `mini-agent/` 内的文本文件 | **安全边界不另写一份**：直接复用 `mini-agent/tools.py::_resolve`，于是"MCP 侧"和"agent 侧"共用同一套三类拒绝（越界 / 敏感文件 / 运行轨迹） |
| `summarize_trace` | 把一份 `logs/run-*.jsonl` 汇总成统计 | **故意只给汇总，不给原文**。把一整段对话历史塞进客户端模型的上下文是另一种泄漏；诊断要的是步数/工具/错误分类/结论行 |
| `check_paper_note` | 跑论文笔记校验脚本 | 演示"server 包确定性脚本"这一形态：脚本内容不进任何人的上下文，只回结果 |

## 协议形状的来源

- 传输与生命周期：<https://modelcontextprotocol.io/specification/2025-06-18/basic/transports>、
  <https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle>
- 字段与错误码：`schema.ts`（同版本的权威类型定义）
  <https://raw.githubusercontent.com/modelcontextprotocol/modelcontextprotocol/main/schema/2025-06-18/schema.ts>
  —— 错误码常量就定义在那里：`PARSE_ERROR=-32700`、`INVALID_REQUEST=-32600`、
  `METHOD_NOT_FOUND=-32601`、`INVALID_PARAMS=-32602`、`INTERNAL_ERROR=-32603`。

**未验证的部分**：本机没有第三方 MCP 客户端（打包资源里没有 SDK，pip 也装不动），
所以配套的 `probe.py` 是**我自己写的客户端** —— 严格说这是"自洽"而不是"互操作"。
字段名逐个对着 `schema.ts` 核过，但"真客户端能连上"这件事还没证明。这条记在 PLAN 里。

## 用法

    python mcp-servers/mini-agent-tools/server.py            # 直接跑：等 stdin 上的 JSON-RPC
    python mcp-servers/mini-agent-tools/probe.py             # 用探针做一轮握手 + 断言
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]  # D:\Agent_test
MINI = ROOT / "mini-agent"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# 复用被测 agent 自己那套边界，不另写一份
sys.path.insert(0, str(MINI))
import tools as mini_tools  # noqa: E402

SERVER_INFO = {"name": "mini-agent-tools", "title": "mini-agent 只读工具", "version": "0.1.0"}
PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
TRACE_NAME = re.compile(r"^run-[0-9]{8}-[0-9]{6}(?:-[0-9]+)?\.jsonl$")
MAX_CHARS = 2000

# 连接状态。规范把生命周期分成 初始化 → 运行 → 关闭 三段，
# 所以"有没有握手过"是**服务端必须记住的唯一状态**。
STATE = {"initialized": False}

# 标准 JSON-RPC 错误码（与 schema.ts 的常量一一对应）
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

TOOLS: list[dict[str, Any]] = [
    {
        "name": "read_project_file",
        "title": "读项目文件（只读）",
        "description": (
            "读取 mini-agent/ 目录内的一个文本文件，返回前 2000 字符。"
            "越界路径、敏感文件（.env 等）、运行轨迹都会被拒绝。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "相对 mini-agent/ 的路径"}},
            "required": ["path"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "summarize_trace",
        "title": "汇总一份运行轨迹",
        "description": (
            "把 logs/run-*.jsonl 汇总成统计：步数、各工具调用次数、错误分类、tokens、"
            "停止原因、结论行。**不返回对话原文**（那会把运行历史灌进你的上下文）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "轨迹文件名，如 run-20261029-215247.jsonl"}},
            "required": ["name"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "check_paper_note",
        "title": "校验论文笔记格式",
        "description": (
            "跑 explaining-papers 的校验脚本，检查一篇论文笔记或 INDEX 是否满足骨架要求。"
            "返回退出码与问题列表。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "相对项目根的 .md 路径"}},
            "required": ["path"],
        },
        "annotations": {"readOnlyHint": True},
    },
]


# ---------------------------------------------------------------------------
# 传输层：一行一条消息
# ---------------------------------------------------------------------------
def log(message: str) -> None:
    """日志只走 stderr。stdout 是协议通道，一个字符都不能多。"""
    print(f"[server] {message}", file=sys.stderr, flush=True)


def send(message: dict[str, Any]) -> None:
    """**唯一写 stdout 的地方。** `ensure_ascii=False` 让中文原样出去，
    `separators` 去掉空格；`json.dumps` 不会产生内嵌换行，所以一条消息一行成立。"""
    sys.stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def reply(req_id: Any, result: dict[str, Any]) -> None:
    send({"jsonrpc": "2.0", "id": req_id, "result": result})


def fail(req_id: Any, code: int, message: str, data: Any = None) -> None:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    send({"jsonrpc": "2.0", "id": req_id, "error": error})


# ---------------------------------------------------------------------------
# 工具实现：失败一律转成 isError=true 的结果，而不是协议层错误
#
# schema.ts 对此有明确要求：**工具自身的错误要放进 result 里并置 isError**，
# 否则模型看不见它、也就没法自我纠正；只有"找不到工具""不支持工具调用"这类
# 异常情况才用协议层 error 响应。
# ---------------------------------------------------------------------------
def text_result(text: str, is_error: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


def tool_read_project_file(args: dict[str, Any]) -> dict[str, Any]:
    path = args.get("path")
    if not isinstance(path, str) or not path:
        return text_result("参数 path 必须是非空字符串", is_error=True)
    try:
        target = mini_tools._resolve(path)  # 三类拒绝都在这一个入口后面
    except Exception as exc:  # noqa: BLE001 —— 拒绝原因原样交给客户端
        return text_result(str(exc), is_error=True)
    if not target.is_file():
        return text_result(f"不是文件：{path}", is_error=True)
    data = target.read_text(encoding="utf-8", errors="replace")
    cut = data[:MAX_CHARS]
    suffix = "" if len(data) <= MAX_CHARS else f"\n…（截断，共 {len(data)} 字符）"
    return text_result(cut + suffix)


def tool_summarize_trace(args: dict[str, Any]) -> dict[str, Any]:
    name = args.get("name")
    if not isinstance(name, str) or not TRACE_NAME.match(name):
        return text_result("参数 name 必须是形如 run-20261029-215247.jsonl 的文件名", is_error=True)
    path = MINI / "logs" / name
    if not path.is_file():
        return text_result(f"没有这份轨迹：{name}", is_error=True)

    head: dict[str, Any] = {}
    steps = 0
    calls: dict[str, int] = {}
    errors: list[str] = []
    tokens = 0
    result_row: dict[str, Any] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        role = row.get("role")
        if role == "run":
            head = row
        elif role == "assistant":
            steps += 1
            tokens += int((row.get("usage") or {}).get("total_tokens") or 0)
            for call in row.get("tool_calls") or []:
                key = str(call.get("name"))
                calls[key] = calls.get(key, 0) + 1
        elif role == "tool" and row.get("is_error"):
            errors.append(str(row.get("error_kind") or "error"))
        elif role == "result":
            result_row = row

    conclusion = None
    for line in reversed((result_row.get("content") or "").splitlines()):
        found = re.search(r"结\s*论\s*[：:]\s*(.+?)\s*$", line)
        if found:
            conclusion = found.group(1)
            break

    summary = {
        "trace": name,
        "model": head.get("model"),
        "tools_given": head.get("tools"),
        "system_prompt_chars": len(head.get("system") or ""),
        "tools_blurred": head.get("tools_blurred"),
        "steps": steps,
        "tool_calls": calls,
        "errors": errors,
        "tokens": tokens or (result_row.get("usage_total") or {}).get("total_tokens"),
        "ok": result_row.get("ok"),
        "reason": result_row.get("reason"),
        "conclusion": conclusion,
    }
    return text_result(json.dumps(summary, ensure_ascii=False, indent=1))


def tool_check_paper_note(args: dict[str, Any]) -> dict[str, Any]:
    path = args.get("path")
    if not isinstance(path, str) or not path:
        return text_result("参数 path 必须是非空字符串", is_error=True)
    target = (ROOT / path).resolve()
    # 只允许项目内的 .md，别把任意路径递给 subprocess
    if ROOT not in target.parents or target.suffix != ".md":
        return text_result("只接受项目根下的 .md 文件", is_error=True)
    if not target.is_file():
        return text_result(f"文件不存在：{path}", is_error=True)
    script = ROOT / ".dsh" / "skills" / "explaining-papers" / "scripts" / "check_note.py"
    done = subprocess.run(
        [sys.executable, str(script), str(target)],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    body = (done.stdout or "") + (done.stderr or "")
    return text_result(f"退出码 {done.returncode}\n{body.strip()}", is_error=done.returncode != 0)


HANDLERS = {
    "read_project_file": tool_read_project_file,
    "summarize_trace": tool_summarize_trace,
    "check_paper_note": tool_check_paper_note,
}


# ---------------------------------------------------------------------------
# 协议分发
# ---------------------------------------------------------------------------
def handle_initialize(req_id: Any, params: dict[str, Any]) -> None:
    wanted = str(params.get("protocolVersion") or "")
    # 规范：server 回报它选的版本；客户端若不支持必须断开。所以"能在不支持时降级"是 client 的事，
    # 我们只承诺"要么用你的版本，要么用我们最新的"。
    version = wanted if wanted in SUPPORTED_VERSIONS else PROTOCOL_VERSION
    reply(req_id, {
        "protocolVersion": version,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": SERVER_INFO,
        "instructions": (
            "这是 mini-agent 项目的只读工具集。读文件用 read_project_file；"
            "看一次运行为什么贵/为什么失败用 summarize_trace；校验论文笔记格式用 check_paper_note。"
        ),
    })


def handle_tools_call(req_id: Any, params: dict[str, Any]) -> None:
    name = params.get("name")
    arguments = params.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(name, str):
        fail(req_id, INVALID_PARAMS, "tools/call 的 params.name 必须是字符串")
        return
    if not isinstance(arguments, dict):
        fail(req_id, INVALID_PARAMS, "tools/call 的 params.arguments 必须是对象")
        return
    handler = HANDLERS.get(name)
    if handler is None:
        # "找不到工具"属于协议层错误（schema.ts 的注释里明确区分了这两种错）
        fail(req_id, INVALID_PARAMS, f"没有名为 {name!r} 的工具", data={"tools": sorted(HANDLERS)})
        return
    try:
        reply(req_id, handler(arguments))
    except Exception as exc:  # noqa: BLE001 —— 工具内部炸了也要回一条合法的消息
        log(f"工具 {name} 抛异常：{exc!r}")
        reply(req_id, text_result(f"工具执行失败：{exc}", is_error=True))


def dispatch(message: dict[str, Any]) -> None:
    method = message.get("method")
    req_id = message.get("id")
    params = message.get("params") or {}

    # 通知（没有 id）：不回任何东西，这是规范要求的
    if req_id is None and method is not None:
        log(f"收到通知 {method}（不回复）")
        return

    # 规范：**初始化必须是客户端与服务器的第一次交互**。所以初始化之前除了
    # initialize 什么都拒。错误码用 -32600（Invalid Request）—— schema.ts 只定义了
    # 五个标准 JSON-RPC 码，没有专门的 "server not initialized" 码，硬造一个不合适。
    if method != "initialize" and not STATE["initialized"]:
        fail(req_id, INVALID_REQUEST, "还没有 initialize：规范要求它是第一个交互")
        return

    if method == "initialize":
        STATE["initialized"] = True
        handle_initialize(req_id, params if isinstance(params, dict) else {})
    elif method == "ping":
        reply(req_id, {})
    elif method == "tools/list":
        reply(req_id, {"tools": TOOLS})
    elif method == "tools/call":
        handle_tools_call(req_id, params if isinstance(params, dict) else {})
    else:
        fail(req_id, METHOD_NOT_FOUND, f"不支持的方法 {method!r}")


def main() -> int:
    log(f"启动：root={ROOT} protocol={PROTOCOL_VERSION}")
    for raw in sys.stdin.buffer:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            # 解析失败时连 id 都没有，按规范回 id:null
            fail(None, PARSE_ERROR, f"JSON 解析失败：{exc.msg}")
            continue
        if not isinstance(message, dict) or "method" not in message:
            fail(message.get("id") if isinstance(message, dict) else None,
                 INVALID_REQUEST, "消息必须是一个带 method 的 JSON 对象")
            continue
        dispatch(message)
    log("stdin 关闭，退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
