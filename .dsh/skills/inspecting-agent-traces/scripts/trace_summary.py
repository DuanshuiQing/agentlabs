"""把一份或一批运行轨迹（`mini-agent/logs/run-*.jsonl`）读成人能看的形状。

## 为什么这是个脚本，而不是写进 SKILL.md 的步骤

轨迹的字段结构是**确定的**（每一行都是 JSON，字段名固定）。
"确定性的事写成脚本" —— 脚本内容不进上下文，模型只需要知道**怎么调、看什么**。
手写 grep 解析这几份 JSONL 是本项目里重复次数最多的浪费：
每次都要重新想 `role` 有哪几种、`tool_calls` 挂在哪、`usage` 在哪个键下。

## 它回答的问题

1. 这次运行**花了多少、几步、停在哪**（`reason` 是 `stop` 还是 `max_steps`）
2. **工具都调了什么、错在哪**（`is_error` / `error_kind` 只在轨迹里有，模型的答案里没有）
3. 每步的 `total_tokens` / `cached` —— 判断"贵"是贵在步数还是贵在上下文
4. **结论行**是什么（答案契约规定的最后一行）
5. 这次运行的**自变量**：给了哪些工具、被模糊的是哪个、有没有 system 提示

## 用法

    python .dsh/skills/inspecting-agent-traces/scripts/trace_summary.py mini-agent/logs/run-20261029-215247.jsonl
    python .../trace_summary.py mini-agent/logs              # 目录 = 里面全部 run-*.jsonl
    python .../trace_summary.py mini-agent/logs -n 3         # 只取最近 3 份
    python .../trace_summary.py mini-agent/logs/run-xxx.jsonl --json
    python .../trace_summary.py mini-agent/logs --errors-only

只依赖标准库。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Windows 上 stdout 被重定向时 Python 会退回 GBK，上游按 UTF-8 解码会乱码
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

CONCLUSION = re.compile(r"结\s*论\s*[：:]\s*(.+?)\s*$")
PREVIEW = 56  # 观察文本 / 题面的预览长度


def load(path: Path) -> tuple[dict, list[dict], dict | None]:
    """读一份轨迹 -> (头部, 中间各行, 结果行)。坏行跳过。"""
    head: dict = {}
    rows: list[dict] = []
    result: dict | None = None
    text = path.read_text(encoding="utf-8-sig")  # 带 BOM 也能读
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        role = rec.get("role")
        if role == "run":
            head = rec
        elif role == "result":
            result = rec
        else:
            rows.append(rec)
    return head, rows, result


def conclusion_of(text: str) -> str | None:
    """按答案契约取**最后一条**「结论：…」。"""
    for line in reversed((text or "").splitlines()):
        found = CONCLUSION.search(line)
        if found:
            return found.group(1)
    return None


def brief(text: object, width: int = PREVIEW) -> str:
    """压成一行，方便终端对齐。"""
    s = " ".join(str(text or "").split())
    return s if len(s) <= width else s[: width - 1] + "…"


def calls_of(rec: dict) -> list[tuple[str, str]]:
    """(工具名, 参数预览)。参数是 JSON 字符串，这里只取前 40 字符。"""
    out = []
    for call in rec.get("tool_calls") or []:
        out.append((call.get("name") or "?", brief(call.get("arguments") or "", 40)))
    return out


def stats(path: Path) -> tuple[dict, dict, list[dict]]:
    """算汇总。**不打印** —— 打印由 show() 负责，否则 --errors-only 会先印后筛。"""
    head, rows, result = load(path)
    steps = [r for r in rows if r.get("role") == "assistant"]
    tools = [r for r in rows if r.get("role") == "tool"]
    calls = [c for r in steps for c in calls_of(r)]
    errors = [r for r in tools if r.get("is_error")]
    tokens = sum(int((r.get("usage") or {}).get("total_tokens") or 0) for r in steps)
    cached = sum(int((r.get("usage") or {}).get("prompt_cache_hit_tokens") or 0) for r in steps)
    answer = (result or {}).get("content") or ""
    data = {
        "trace": path.name,
        "model": head.get("model"),
        "max_steps": head.get("max_steps"),
        "tools_active": head.get("tools") or [],
        "tools_blurred": head.get("tools_blurred") or {},
        "system_chars": len(head.get("system") or ""),
        "task": brief(head.get("content"), 120),
        "steps": len(steps),
        "tool_calls": len(calls),
        "tools_used": sorted({c[0] for c in calls}),
        "errors": len(errors),
        "error_kinds": sorted({str(r.get("error_kind") or "?") for r in errors}),
        "tokens": tokens or int(((result or {}).get("usage_total") or {}).get("total_tokens") or 0),
        "cached_tokens": cached,
        "ok": (result or {}).get("ok"),
        "reason": (result or {}).get("reason"),
        "conclusion": conclusion_of(answer),
    }
    return data, head, rows


def show(data: dict, rows: list[dict]) -> None:
    print(f"trace    {data['trace']}")
    print(f"model    {data['model']}   max_steps={data['max_steps']}")
    print(f"tools    {', '.join(data['tools_active']) or '-'}")
    print(f"system   {data['system_chars']} 字符" if data["system_chars"] else "system   无")
    if data["tools_blurred"]:
        print(f"模糊     {data['tools_blurred']}")
    print(f"task     {data['task']}")
    print("-" * 78)
    for rec in rows:
        role = rec.get("role")
        step = rec.get("step", "?")
        if role == "assistant":
            names = ", ".join(f"{n}({a})" for n, a in calls_of(rec)) or "（不调工具，收尾）"
            usage = rec.get("usage") or {}
            money = f"  {usage.get('total_tokens', 0)} tok" if usage else ""
            print(f"  #{step:<3} 模型 -> {names}{money}")
        elif role == "tool":
            if rec.get("is_error"):
                mark = "✗ " + str(rec.get("error_kind") or "error")
            else:
                mark = "ok"
            print(f"         工具 {rec.get('tool_name')} {mark}  {brief(rec.get('content'))}")
        elif role in ("system", "user"):
            print(f"  #{step:<3} {role}  {brief(rec.get('content'))}")
    print("-" * 78)
    print(
        f"合计     {data['steps']} 步 / {data['tool_calls']} 次工具调用 / "
        f"{data['tokens']} tokens（cached {data['cached_tokens']}）"
    )
    kinds = f"  类型={data['error_kinds']}" if data["error_kinds"] else ""
    print(f"错误     {data['errors']}{kinds}")
    print(f"结果     ok={data['ok']}  reason={data['reason']}")
    print(f"结论     {data['conclusion']}" if data["conclusion"] else "结论     （没有 `结论：…` 行）")


def collect(paths: list[str], newest: int | None) -> list[Path]:
    found: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            found.extend(sorted(p.glob("run-*.jsonl")))
        elif p.exists():
            found.append(p)
        else:
            found.extend(sorted(Path().glob(raw)))
    found = sorted(dict.fromkeys(found))
    return found[-newest:] if newest else found


def main() -> int:
    parser = argparse.ArgumentParser(description="把运行轨迹读成人能看的形状")
    parser.add_argument("paths", nargs="+", help="轨迹文件、目录（里面全部 run-*.jsonl）或 glob")
    parser.add_argument("-n", "--newest", type=int, help="只取最近 N 份")
    parser.add_argument("--json", action="store_true", help="输出 JSON（给别的脚本用）")
    parser.add_argument("--errors-only", action="store_true", help="只列有工具错误的轨迹")
    args = parser.parse_args()

    files = collect(args.paths, args.newest)
    if not files:
        print("没有找到轨迹文件。")
        return 1

    reports = [stats(p) for p in files]
    if args.errors_only:
        reports = [r for r in reports if r[0]["errors"]]
        if not reports:
            print("没有带工具错误的轨迹。")
            return 0

    if args.json:
        flat = [r[0] for r in reports]
        print(json.dumps(flat if len(flat) > 1 else flat[0], ensure_ascii=False, indent=2))
        return 0

    for i, (data, _head, rows) in enumerate(reports):
        if i:
            print()
        show(data, rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
