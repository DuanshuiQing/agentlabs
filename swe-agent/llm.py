"""调用模型：发一次请求，把 SDK 的原始响应原封不动交出来。

**来源**：复制自 `mini-agent/llm.py`（同一作者、同一工作区的阶段 0 产物）。
差异只有两处：

  1. 密钥读取顺序：先 `swe-agent/.env`，没有再回退 `../mini-agent/.env`
     —— 不为了新项目再存一份明文密钥。
  2. 注释按本项目重写；接口约定**一个字没改**（`call_model(messages, tools)`）。

接口约定（不要改）：不做任何解析、不做循环、必须带 timeout。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent
DEFAULT_ENV = HERE.parent / "mini-agent" / ".env"


def _load_env() -> str:
    from dotenv import load_dotenv

    for cand in (HERE / ".env", DEFAULT_ENV):
        if cand.exists():
            load_dotenv(cand)
            return str(cand)
    return ""


ENV_SOURCE = _load_env()

from openai import OpenAI  # noqa: E402 —— 必须在 load_dotenv 之后导入也无所谓，客户端是惰性的

MODEL_TIMEOUT = 60.0  # 秒。不设的话网络卡住会无限挂起
MODEL = os.getenv("MODEL", "").strip()
BASE_URL = os.getenv("OPENAI_BASE_URL", "").strip() or None
API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

_client: OpenAI | None = None


def get_client() -> OpenAI:
    """惰性创建：`import llm` 不该连网、也不该因为缺密钥就崩。"""
    global _client
    if _client is None:
        if not API_KEY:
            raise RuntimeError(f"没有读到 OPENAI_API_KEY（已尝试 {ENV_SOURCE or '无 .env'}）")
        _client = OpenAI(api_key=API_KEY, base_url=BASE_URL, timeout=MODEL_TIMEOUT)
    return _client


def call_model(messages: list[dict], tools: list[dict] | None = None) -> Any:
    """发一次请求，返回 SDK 原始响应对象。

    Args:
        messages: 对话历史
        tools: 工具的 JSON Schema 清单；None 表示这一轮不给工具

    Returns:
        SDK 原始响应。**故意不解析** —— 解析属于循环的职责。
    """
    kwargs: dict[str, Any] = {"model": MODEL, "messages": messages}
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    return get_client().chat.completions.create(**kwargs)


if __name__ == "__main__":
    print(f"MODEL  : {MODEL}")
    print(f"BASE   : {BASE_URL}")
    print(f"env 来自: {ENV_SOURCE}")
    resp = call_model([{"role": "user", "content": "只回复两个字：就绪"}])
    msg = resp.choices[0].message
    print(f"回复   : {msg.content!r}")
    print(f"tokens : {resp.usage.total_tokens if resp.usage else '?'}")
