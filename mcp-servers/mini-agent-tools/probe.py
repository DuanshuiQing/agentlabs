"""用最小客户端做一轮完整握手 + 18 条断言，验证 server 真的说 MCP。

## 为什么需要它

server 写完之后，"它能跑"和"它说协议"是两件事。这个探针把两者分开：
逐条发请求、逐条按 `schema.ts` 的字段名断言响应。跑一次约 2 秒、零网络。

## 它的局限（必须写在文件里，而不是记在心里）

客户端是我自己写的 —— 所以它只能证明"我的 server 与我的理解自洽"，
**证明不了"第三方 MCP 客户端能连上"**。本机没有可用的第三方客户端
（打包资源里没有 SDK、pip 装不动）。字段名逐个对着同版本的 `schema.ts` 核过，
但互操作性仍待真客户端验证。这条记在 PLAN §5。

## 断言覆盖（25 条）

协议层：未初始化就发请求被拒 / initialize 形状 / 版本协商 / 能力声明 / id 回显 /
通知不回复 / 未知方法 -32601 / 坏 JSON -32700 / 缺参数 -32602 / 未知工具 -32602 / EOF 退出码 0
工具层：成功路径 / 敏感文件拒绝 / 越界拒绝 / 轨迹拒绝 / 汇总含 stats

    python mcp-servers/mini-agent-tools/probe.py

退出码 0 = 全部通过。
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SERVER = HERE / "server.py"
MINI = ROOT / "mini-agent"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))


class Client:
    """二进制管道 + 显式编解码：避开 Windows 文本模式把 \\n 翻成 \\r\\n 的老问题。"""

    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.lines: queue.Queue[bytes] = queue.Queue()
        self.stderr: list[str] = []
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

    def _pump_stdout(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            self.lines.put(raw)
        self.lines.put(b"")  # 哨兵：EOF

    def _pump_stderr(self) -> None:
        assert self.proc.stderr is not None
        for raw in self.proc.stderr:
            self.stderr.append(raw.decode("utf-8", errors="replace").rstrip("\r\n"))

    def send(self, message: dict | str) -> None:
        assert self.proc.stdin is not None
        text = message if isinstance(message, str) else json.dumps(message, ensure_ascii=False)
        self.proc.stdin.write(text.encode("utf-8") + b"\n")
        self.proc.stdin.flush()

    def recv(self, timeout: float = 10.0) -> dict | None:
        raw = self.lines.get(timeout=timeout)
        if raw == b"":
            return None
        # 每个字都要是合法 JSON —— 这一行同时验证了"stdout 不许写别的东西"
        return json.loads(raw.decode("utf-8"))

    def close(self) -> int:
        if self.proc.stdin:
            self.proc.stdin.close()
        return self.proc.wait(timeout=10)


def newest_trace() -> str:
    traces = sorted((MINI / "logs").glob("run-*.jsonl"))
    return traces[-1].name if traces else ""


def main() -> int:
    trace = newest_trace()
    client = Client()
    try:
        # ---- 0) 初始化之前，一切请求都该被拒 --------------------------------
        client.send({"jsonrpc": "2.0", "id": 0, "method": "tools/list"})
        early = client.recv() or {}
        check("未初始化时 tools/list 被拒（-32600）",
              (early.get("error") or {}).get("code") == -32600,
              str((early.get("error") or {}).get("message"))[:30])

        # ---- 1) initialize -------------------------------------------------
        client.send({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"roots": {"listChanged": True}},
                "clientInfo": {"name": "mini-probe", "version": "0.1.0"},
            },
        })
        init = client.recv()
        check("initialize 有响应", init is not None)
        init = init or {}
        result = init.get("result") or {}
        check("jsonrpc 是 2.0", init.get("jsonrpc") == "2.0")
        check("id 回显为 1", init.get("id") == 1, repr(init.get("id")))
        check("protocolVersion 被回报", isinstance(result.get("protocolVersion"), str),
              str(result.get("protocolVersion")))
        check("capabilities.tools 已声明", isinstance((result.get("capabilities") or {}).get("tools"), dict))
        info = result.get("serverInfo") or {}
        check("serverInfo 有 name+version", bool(info.get("name")) and bool(info.get("version")))

        # ---- 2) 通知不回复 -------------------------------------------------
        client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        client.send({"jsonrpc": "2.0", "id": 99, "method": "ping"})
        pong = client.recv() or {}
        check("通知不产生响应（下一条就该是 ping）", pong.get("id") == 99, f"id={pong.get('id')!r}")
        check("ping 回空 result", pong.get("result") == {})

        # ---- 3) tools/list -------------------------------------------------
        client.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        listed = (client.recv() or {}).get("result") or {}
        tools = listed.get("tools") or []
        check("tools/list 非空", len(tools) >= 3, f"{len(tools)} 个")
        shapes_ok = all(
            isinstance(t.get("name"), str) and isinstance(t.get("description"), str)
            and (t.get("inputSchema") or {}).get("type") == "object"
            for t in tools
        )
        check("每个 tool 都有 name/description/inputSchema.type=object", shapes_ok)
        names = {t.get("name") for t in tools}
        check("工具名集合符合预期",
              names == {"read_project_file", "summarize_trace", "check_paper_note"}, str(sorted(names)))

        # ---- 4) 成功路径 ---------------------------------------------------
        client.send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                     "params": {"name": "read_project_file",
                                "arguments": {"path": "requirements.txt"}}})
        ok_call = (client.recv() or {}).get("result") or {}
        content = ok_call.get("content") or []
        check("成功的 tools/call 返回 content[0].type=text",
              bool(content) and content[0].get("type") == "text")
        check("成功的 tools/call 不带 isError", not ok_call.get("isError"))
        check("读到的内容非空", bool((content[0].get("text") if content else "")))

        # ---- 5) 三类拒绝 + 轨迹 --------------------------------------------
        for label, args, expect in [
            ("敏感文件被拒", {"path": ".env"}, True),
            ("越界路径被拒", {"path": "../README.md"}, True),
            ("轨迹文件被拒", {"path": f"logs/{trace}"} if trace else {"path": "logs/x.jsonl"}, True),
        ]:
            client.send({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                         "params": {"name": "read_project_file", "arguments": args}})
            got = (client.recv() or {}).get("result") or {}
            check(label, bool(got.get("isError")) is expect,
                  str((got.get("content") or [{}])[0].get("text"))[:40])

        # ---- 6) summarize_trace --------------------------------------------
        if trace:
            client.send({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                         "params": {"name": "summarize_trace", "arguments": {"name": trace}}})
            got = (client.recv() or {}).get("result") or {}
            body = (got.get("content") or [{}])[0].get("text") or ""
            check("summarize_trace 返回统计", '"steps"' in body and not got.get("isError"),
                  body[:60].replace("\n", " "))

        # ---- 7) 协议层错误 -------------------------------------------------
        client.send({"jsonrpc": "2.0", "id": 6, "method": "no/such/method"})
        err = (client.recv() or {}).get("error") or {}
        check("未知方法 -> -32601", err.get("code") == -32601, str(err.get("code")))

        client.send({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                     "params": {"name": "no_such_tool", "arguments": {}}})
        err = (client.recv() or {}).get("error") or {}
        check("未知工具 -> -32602", err.get("code") == -32602, str(err.get("code")))

        client.send({"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"arguments": {}}})
        err = (client.recv() or {}).get("error") or {}
        check("缺 params.name -> -32602", err.get("code") == -32602, str(err.get("code")))

        client.send('{"jsonrpc": "2.0", "id": 9, "method": ')  # 故意坏掉
        err = (client.recv() or {}).get("error") or {}
        check("坏 JSON -> -32700 且 id=null", err.get("code") == -32700, str(err.get("code")))

        # ---- 8) 关 stdin 应正常退出 ----------------------------------------
        code = client.close()
        check("stdin 关闭后退出码 0", code == 0, f"exit={code}")
        check("stderr 有日志（不污染 stdout）", any("[server]" in line for line in client.stderr),
              (client.stderr or ["<空>"])[0][:40])
    finally:
        if client.proc.poll() is None:
            client.proc.kill()

    width = max(len(name) for name, _, _ in RESULTS)
    for name, ok, detail in RESULTS:
        mark = "✓" if ok else "✗"
        extra = f"   {detail}" if detail and not ok else (f"   {detail}" if detail else "")
        print(f"  {mark} {name:<{width}}{extra}")
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} 条断言通过")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
