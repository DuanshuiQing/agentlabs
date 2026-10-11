"""阶段 0 环境自检：确认 Python、虚拟环境、依赖、密钥、网络五件事都通了。

用法（用 venv 里的解释器，不要用裸 python）：

    mini-agent\\.venv\\Scripts\\python.exe mini-agent\\check_env.py

它的唯一作用是**把「环境问题」和「你写的 agent 逻辑问题」分开** ——
以后 agent 出问题时，你能确定问题在自己的代码里，而不是环境。
跑通它不代表阶段 0 完成，完成定义见 PLAN.md §4.1。
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time

# Windows 上当 stdout 不是终端（被重定向或被别的程序捕获）时，Python 会退回
# 系统区域编码（中文环境是 GBK），而上游往往按 UTF-8 解码 → 中文全变乱码。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

MIN_PY = (3, 11)
API_TIMEOUT = 60.0  # 单次模型调用超时（秒）。不设的话网络卡住会无限挂起
HERE = os.path.dirname(os.path.abspath(__file__))


def mask(secret: str) -> str:
    """把密钥遮起来，避免截图或日志泄露。"""
    if len(secret) <= 10:
        return "*" * len(secret)
    return f"{secret[:6]}...{secret[-4:]} (len={len(secret)})"


def _installed(module: str) -> str | None:
    """返回模块版本或 None（避免真的 import 进来，快且无副作用）。"""
    spec = importlib.util.find_spec(module)
    if spec is None:
        return None
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(module)
    except PackageNotFoundError:
        return "已安装（版本未知）"


def check_python(problems: list[str]) -> None:
    v = sys.version_info
    print(f"[1/5] Python {v.major}.{v.minor}.{v.micro}")
    print(f"      解释器: {sys.executable}")
    if (v.major, v.minor) < MIN_PY:
        problems.append(f"Python 版本过低，需要 >= {MIN_PY[0]}.{MIN_PY[1]}")


def check_venv(problems: list[str]) -> None:
    print("[2/5] 虚拟环境")
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    print(f"      sys.prefix : {sys.prefix}")
    print(f"      base_prefix: {sys.base_prefix}")
    if not in_venv:
        print("      是否在 venv: 否  <-- 你现在用的是系统解释器！")
        problems.append(
            "不在虚拟环境里。请改用 mini-agent\\.venv\\Scripts\\python.exe 运行本脚本"
        )
        return

    print("      是否在 venv: 是 (OK)")
    # 这一步专门抓「用 --without-pip 建的坏 venv」：
    # 那种 venv 里没有 pip，于是 pip install 会装到全局解释器，
    # 而运行时又看不到，症状是无法解释的 ModuleNotFoundError。
    if _installed("pip") is None:
        print("      自带 pip   : 没有  <-- 这个 venv 是坏的！")
        problems.append(
            "venv 里没有 pip（很可能建的时候加了 --without-pip）。"
            "修法：.venv\\Scripts\\python.exe -m ensurepip --upgrade"
        )
    else:
        print(f"      自带 pip   : {_installed('pip')} (OK)")


def check_deps(problems: list[str]) -> None:
    print("[3/5] 依赖包")
    for module, hint in (
        ("openai", "pip install -r requirements.txt"),
        ("dotenv", "pip install -r requirements.txt"),
    ):
        got = _installed(module)
        if got is None:
            print(f"      {module:<10} 未安装")
            problems.append(f"缺少 {module}，请运行: {hint}")
        else:
            print(f"      {module:<10} {got}")


def check_config(problems: list[str]) -> tuple[str, str, str]:
    print("[4/5] 配置")
    env_path = os.path.join(HERE, ".env")
    if _installed("dotenv") is not None:
        from dotenv import load_dotenv

        if os.path.exists(env_path):
            load_dotenv(env_path)
            print("      已读取 .env")
        else:
            print("      未找到 .env  (从 .env.example 复制一份，或改用系统环境变量)")

    key = os.getenv("OPENAI_API_KEY", "").strip()
    base_url = os.getenv("OPENAI_BASE_URL", "").strip()
    model = os.getenv("MODEL", "").strip()

    if key and not key.startswith("sk-在这里"):
        print(f"      OPENAI_API_KEY : {mask(key)}")
    else:
        print("      OPENAI_API_KEY : 未设置（或还是模板里的占位符）")
        problems.append("没有可用的 OPENAI_API_KEY")

    print(f"      OPENAI_BASE_URL: {base_url or '未设置（默认走 api.openai.com，国内通常不通）'}")
    print(f"      MODEL          : {model or '未设置'}")
    if not model:
        problems.append("没有设置 MODEL")
    return key, base_url, model


def check_roundtrip(key: str, base_url: str, model: str, problems: list[str]) -> None:
    print("[5/5] 真实接口往返")
    if problems:
        print("      跳过（先解决上面的问题）")
        return
    try:
        from openai import OpenAI

        client = OpenAI(api_key=key, base_url=base_url or None, timeout=API_TIMEOUT)
        t0 = time.time()
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "只回复两个字：就绪"}],
            temperature=0,
        )
        dt = time.time() - t0
        text = (resp.choices[0].message.content or "").strip()
        print(f"      模型回复: {text!r}")
        print(f"      耗时    : {dt:.2f}s")
        print("      结论    : 接口连通 OK")
    except Exception as exc:  # noqa: BLE001 —— 这里就要把各种错误都摊出来看
        msg = str(exc)
        print(f"      失败: {type(exc).__name__}: {msg[:300]}")
        hint = _hint_for(msg)
        if hint:
            print(f"      可能原因: {hint}")
        problems.append("接口往返失败")


def _hint_for(msg: str) -> str:
    m = msg.lower()
    if "401" in m or ("invalid" in m and "key" in m) or "authentication" in m:
        return "密钥无效或已失效，去平台重新生成一个"
    if "404" in m or ("model" in m and "not" in m):
        return "base_url 或 MODEL 名字不对，去平台的模型列表页核对当前可用的模型名"
    if "402" in m or "insufficient" in m or "balance" in m or "quota" in m:
        return "余额不足或额度用尽，去平台充值"
    if "429" in m or "rate" in m:
        return "触发限流，等几十秒再试"
    if "connect" in m or "timeout" in m or "ssl" in m or "proxy" in m:
        return "网络不通，检查代理 / 换个国内可直连的平台"
    return ""


def main() -> int:
    problems: list[str] = []
    check_python(problems)
    check_venv(problems)
    check_deps(problems)
    key, base_url, model = check_config(problems)
    check_roundtrip(key, base_url, model, problems)

    print("-" * 56)
    if problems:
        print("环境未就绪，需处理：")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("环境就绪 OK —— 可以开始写 agent 了。")
    print("提醒：这只证明环境可用；阶段 0 的完成定义见 PLAN.md §4.1。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
