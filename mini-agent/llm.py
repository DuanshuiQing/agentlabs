"""阶段 0 · 第 1 步：把「调用模型」单独抽出来。

这一步**还没有 Agent** —— 它只做一件事：发一次请求，把模型返回的
原始响应对象原封不动交出来。

为什么第一步是这个：把「网络 / 密钥 / SDK 用法」这层不确定性先隔离干净，
后面每一步出问题时，你才能确定 bug 一定在自己的循环逻辑里，而不是环境。
这正是 check_env.py 存在的意义，只不过这里把它固化成**可复用的模块接口**。

接口约定（PLAN §4.3，后面每一步都依赖它，不要改）：
    call_model(messages, tools) -> 原始响应对象
    不做任何解析、不做任何循环。

运行（必须用 venv 里的解释器）：
    mini-agent\\.venv\\Scripts\\python.exe mini-agent\\llm.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# Windows 上 stdout 被重定向时 Python 会退回 GBK，上游按 UTF-8 解码会乱码
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv
from openai import OpenAI

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")

MODEL_TIMEOUT = 60.0  # 单次模型调用超时（秒）。不设的话网络卡住会无限挂起
MODEL = os.getenv("MODEL", "").strip()
BASE_URL = os.getenv("OPENAI_BASE_URL", "").strip() or None
API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

_client: OpenAI | None = None


def get_client() -> OpenAI:
    """惰性创建客户端。

    故意不放在模块顶层：那样 `import llm` 就会连网/校验密钥，
    而 agent.py 只是想把 call_model 当函数用，不该有这种副作用。
    """
    global _client
    if _client is None:
        if not API_KEY:
            raise RuntimeError("没有读到 OPENAI_API_KEY，检查 mini-agent/.env")
        _client = OpenAI(api_key=API_KEY, base_url=BASE_URL, timeout=MODEL_TIMEOUT)
    return _client


def call_model(messages: list[dict], tools: list[dict] | None = None) -> Any:
    """发一次请求，返回 SDK 的原始响应对象。

    Args:
        messages: 对话历史，形如 [{"role": "user", "content": "..."}]
        tools: 工具的 JSON Schema 清单。第 1 步传 None，第 2 步才会用上。

    Returns:
        SDK 原始响应对象。**故意不解析** —— 解析属于 agent 的职责，
        放在这里会让第 2、3 步看不见真实的数据结构。
    """
    kwargs: dict[str, Any] = {"model": MODEL, "messages": messages}
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    return get_client().chat.completions.create(**kwargs)


def dump_response(resp: Any) -> None:
    """把响应对象的结构摊开 —— 第 1 步的全部意义就是看清它。"""
    msg = resp.choices[0].message
    print("=== 响应对象的形状 ===")
    print(f"resp.id                  : {resp.id}")
    print(f"resp.model               : {resp.model}")
    print(f"len(resp.choices)        : {len(resp.choices)}")
    print(f"choices[0].finish_reason : {resp.choices[0].finish_reason!r}")
    print(f"message.role             : {msg.role!r}")
    print(f"message.content          : {msg.content!r}")
    print(f"message.tool_calls       : {msg.tool_calls!r}")
    print("        ^^^ 第 2 步给了 tools 之后，它才会变成非空列表")
    if resp.usage:
        u = resp.usage
        print(
            f"usage                    : "
            f"prompt={u.prompt_tokens} completion={u.completion_tokens} "
            f"total={u.total_tokens}"
        )
        print("        ^^^ 这三个数字就是你的钱。阶段 1 做 token 成本核算看的就是它")

    print("\n=== 同一个 message 序列化成 JSON（这才是要 append 回 messages 的东西）===")
    print(json.dumps(msg.model_dump(exclude_none=True), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    print(f"MODEL    : {MODEL}")
    print(f"BASE_URL : {BASE_URL}")
    print(f"超时     : {MODEL_TIMEOUT}s\n")
    try:
        response = call_model([{"role": "user", "content": "只回复两个字：就绪"}])
        dump_response(response)
    except Exception as exc:  # noqa: BLE001 —— 这一步就要把错误原样摊出来
        print(f"调用失败: {type(exc).__name__}: {exc}")
        raise SystemExit(1)
