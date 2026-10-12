"""阶段 0 · 第 3 步 + 第 5 步：工具注册表 + dispatch —— 把「模型的请求」变成「一段文本观察」。

第 2 步我们只让模型"说出"它想调什么，没有执行；第 3 步补上执行。
（第 2 步那个只打印 tool_calls 的探针已被 agent.py 完整取代，故移除。）
第 5 步只改一件事：**错误文本长什么样**。

三条铁律（第 3 步的全部内容）：

  1. **dispatch 一律返回字符串，绝不向外抛异常。**
     模型是靠读文本改变策略的；抛出异常等于把它掐断，它就没机会换条路。

  2. **校验必须发生在调用之前。**
     SDK 自己在 `Function.arguments` 的文档里警告过：
     "the model does not always generate valid JSON, and may hallucinate
      parameters not defined by your function schema. Validate the arguments
      in your code before calling your function."
     —— 所以"参数 JSON 解析失败""参数名不存在""缺必填参数"都不是理论风险。

  3. **schema 与校验规则只能有一个数据源。**
     下面用 ToolSpec 同时生成「给模型看的 schema」和「dispatch 用的校验」。
     如果手工维护两份，第 5、8 步再加工具时必然不一致 ——
     而不一致的后果是：模型按 schema 填参数，dispatch 却把它判为非法。

第 5 步补的两条：

  4. **异常文本不是给人看的，是给模型看的。**
     原来那句 `ERROR: {异常类名}: {原始报错}` 会把主机绝对路径原样送进模型上下文，
     还带着 repr 转义出来的双反斜杠。四个后果：泄露主机目录结构、白烧 token、
     换台机器就不再复现、模型可能照抄那个绝对路径去重试。
     改法不是"用正则把路径洗掉" —— 黑名单永远漏。改法是**白名单**：
     只对认得出来的异常类型，输出**我们自己拼的**文本；认不出来就默认丢掉原始信息。

  5. **超时不是"杀掉"工具，是"不再等它"。**
     Python 没有安全的线程强杀，被放弃的那个函数可能还在后台跑。
     用 daemon 线程 + join(timeout) 至少保证它不拖住进程退出。
     （不能用 signal.alarm：Windows 上没有 SIGALRM。）

运行（纯本地，不发 API 请求）：
    mini-agent\\.venv\\Scripts\\python.exe mini-agent\\tools.py
"""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# Windows 上 stdout 被重定向时 Python 会退回 GBK，上游按 UTF-8 解码会乱码
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 工具路径的基准目录 = mini-agent 目录。
# 这样 PLAN §4.4 的任务文本（"读取 data/handbook.txt"）可以原样使用。
ROOT = Path(__file__).resolve().parent
MAX_CHARS = 2000


class PathEscape(PermissionError):
    """路径越出了 mini-agent 目录。

    单独一个类，是为了让 _describe_failure 能一眼认出它 —— 它和操作系统抛的
    PermissionError 必须区别对待：我们的消息是我们自己写的，不含主机路径；
    操作系统的消息里带着绝对路径，绝不能透传给模型。
    """


def _resolve(path: str) -> Path:
    """把工具收到的相对路径解析成绝对路径，并拒绝越出 mini-agent 目录。"""
    target = (ROOT / path).resolve()
    if target != ROOT and ROOT not in target.parents:
        raise PathEscape(f"路径越界：{path} 不在 mini-agent 目录内")
    return target


def read_file(path: str) -> str:
    """读取 mini-agent 目录内的一个文本文件。

    ⚠️ 这个函数是给 dispatch 调用的。模型看不见它，也执行不了它。
    """
    target = _resolve(path)
    # 自己先判断"这是不是目录"，不要指望操作系统的 errno 跨平台一致：
    # 对同一个目录调用 read_text()，Windows 抛 PermissionError，Linux 抛 IsADirectoryError。
    if target.is_dir():
        raise IsADirectoryError(f"{path} 是目录，不是文件")
    text = target.read_text(encoding="utf-8")
    if len(text) > MAX_CHARS:
        return text[:MAX_CHARS] + f"\n...（已截断，原文件共 {len(text)} 字符）"
    return text


# ---------------------------------------------------------------------------
# 工具注册表：schema 与校验的唯一数据源
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    """一个工具的全部信息。"""

    name: str
    description: str
    params: dict[str, str]  # 参数名 -> 给模型看的说明
    required: tuple[str, ...]
    func: Callable[..., str]

    def schema(self) -> dict:
        """生成发给模型的 JSON Schema。不手工维护第二份。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        p: {"type": "string", "description": d}
                        for p, d in self.params.items()
                    },
                    "required": list(self.required),
                },
            },
        }

    def validate(self, args: object) -> str | None:
        """校验参数。返回错误说明字符串；None 表示通过。

        注意：现在所有参数都是字符串。将来若出现 int/bool 参数，
        这里需要按类型扩展 —— 那一步我会明确说明。
        """
        if not isinstance(args, dict):
            return f"参数必须是一个 JSON 对象，收到的是 {type(args).__name__}"

        missing = [k for k in self.required if k not in args]
        if missing:
            return f"缺少必填参数 {missing}；本工具需要 {list(self.required)}"

        unknown = [k for k in args if k not in self.params]
        if unknown:
            return f"出现 schema 里没有的参数 {unknown}；本工具只接受 {list(self.params)}"

        wrong = [k for k, v in args.items() if not isinstance(v, str)]
        if wrong:
            return f"参数 {wrong} 必须是字符串"

        return None


_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="read_file",
        # description 是模型选不选这个工具的唯一依据，必须和实现的真实边界一致。
        description=(
            "读取 mini-agent 目录内的一个文本文件，返回其内容（最多前 2000 字符）。"
            "当需要知道某个文件里写了什么时使用。"
            "path 必须是相对 mini-agent 目录的路径，例如 'requirements.txt'。"
        ),
        params={"path": "相对 mini-agent 目录的文件路径，例如 'requirements.txt'"},
        required=("path",),
        func=read_file,
    ),
)

_REGISTRY: dict[str, ToolSpec] = {spec.name: spec for spec in _TOOL_SPECS}

# 模型能看见的全部内容就是这个列表，它由上面的 ToolSpec 生成
TOOLS: list[dict] = [spec.schema() for spec in _TOOL_SPECS]


# ---------------------------------------------------------------------------
# 第 5 步：超时 + 把异常翻译成「给模型看的文本」
# ---------------------------------------------------------------------------

TOOL_TIMEOUT = 20.0  # PLAN §4.1 第 5 条：单次工具调用 20 秒


class ToolTimeout(Exception):
    """工具超过 TOOL_TIMEOUT 仍未返回。"""

    def __init__(self, name: str, timeout: float) -> None:
        super().__init__(f"工具 {name!r} 超过 {timeout:g} 秒未返回")
        self.name = name
        self.timeout = timeout


class Observation(str):
    """一段回喂给模型的观察文本 —— 它同时**是** `str`，所以铁律没有被破坏。

    第 5 步把异常类名从给模型的文本里摘掉了（那是噪声，还可能夹着主机路径）。
    代价是"**为什么**失败"这个信息在返回值里彻底没了。
    第 7 步的 JSONL 需要它，于是用 `str` 的子类把结构化字段挂在文本上：

        模型看到的   —— 就是这段字符串本身，一个字符都没变
        复盘代码看到 —— 还能多读到 error_kind / error_type

    `isinstance(obs, str)` 依然为真，所以 dispatch"一律返回字符串"这条依然成立。
    这是一个通用手法：**接口的承诺可以保持窄，额外信息搭在车顶上。**
    """

    error_kind: str | None = None  # 我们自己的词表：稳定、可聚合、可统计
    error_type: str | None = None  # Python 异常类名：排查时用

    @classmethod
    def failure(cls, text: str, kind: str, error_type: str | None = None) -> "Observation":
        obs = cls(text)
        obs.error_kind = kind
        obs.error_type = error_type
        return obs


def _run_tool(spec: ToolSpec, args: dict, timeout: float) -> str:
    """跑一次工具，最多等 timeout 秒；超了抛 ToolTimeout。"""
    box: dict = {}

    def target() -> None:
        try:
            box["value"] = spec.func(**args)
        except BaseException as exc:  # 在工作线程里兜住，带回主线程再判断
            box["error"] = exc

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(timeout)

    if worker.is_alive():
        raise ToolTimeout(spec.name, timeout)
    if "error" in box:
        raise box["error"]
    return box["value"]


def _describe_failure(exc: Exception, name: str, args: object) -> str:
    """把异常翻译成一段**给模型看的**文本。

    白名单，不是清洗：只有认得出来的异常才带细节，而且那个细节是用
    **模型自己传进来的相对路径**拼的。认不出来就一句话打发 ——
    原始 message 一概不透传，因为它可能含主机绝对路径。
    """
    path = args.get("path") if isinstance(args, dict) else None
    at = f"：{path}" if isinstance(path, str) else ""

    if isinstance(exc, ToolTimeout):
        return (
            f"工具 {name!r} 超时，超过 {exc.timeout:g} 秒仍未返回，已放弃等待。"
            "可以换个做法，或如实说明这一步失败了。"
        )
    if isinstance(exc, PathEscape):  # 必须排在 PermissionError 前面
        return str(exc)
    if isinstance(exc, FileNotFoundError):
        return f"文件不存在{at}（本项目路径都相对 mini-agent 目录写，不带盘符）"
    if isinstance(exc, IsADirectoryError):
        return f"{path} 是目录，不是文件，请给出文件路径"
    if isinstance(exc, NotADirectoryError):
        return f"路径中间有一段不是目录{at}"
    if isinstance(exc, PermissionError):
        return f"读不了{at}（路径必须留在 mini-agent 目录内）"
    if isinstance(exc, UnicodeDecodeError):
        return f"{path} 不是 UTF-8 文本，读不成文本"
    return f"工具 {name!r} 执行失败（{type(exc).__name__}）。原始报错已省略：它可能含主机路径。"


def dispatch(name: str, args: dict, timeout: float = TOOL_TIMEOUT) -> str:
    """执行工具。必须返回字符串；任何异常都转成字符串返回，绝不向外抛。

    Args:
        name: 模型请求的工具名
        args: 已经解析好的参数字典。JSON 解析由 agent 层负责 ——
              tools 层不该知道模型输出的线上格式。
        timeout: 单次工具超时秒数。默认值留在本模块，agent 层不需要知道它。
    """
    spec = _REGISTRY.get(name)
    if spec is None:
        return Observation.failure(
            f"ERROR: 没有名为 {name!r} 的工具。可用工具：{sorted(_REGISTRY)}",
            "unknown_tool",
        )

    problem = spec.validate(args)
    if problem is not None:
        return Observation.failure(f"ERROR: 参数不合法 —— {problem}", "bad_arguments")

    try:
        result = _run_tool(spec, args, timeout)
    except Exception as exc:  # noqa: BLE001 —— 故意兜住一切，转成文本回喂
        # 注意这里**不把异常类名写进给模型的文本**：那是噪声，还可能夹着主机路径。
        # 但它不能丢 —— 搭在 Observation 的车顶上，第 7 步落进 JSONL 供复盘。
        kind = "tool_timeout" if isinstance(exc, ToolTimeout) else "tool_raised"
        return Observation.failure(
            f"ERROR: {_describe_failure(exc, name, args)}", kind, type(exc).__name__
        )

    if not isinstance(result, str):
        return Observation.failure(
            f"ERROR: 工具 {name!r} 返回了 {type(result).__name__}，dispatch 只能返回字符串",
            "bad_return_type",
        )
    return result


# ---------------------------------------------------------------------------
# 失败路径自检 —— 第 3 步"测什么"的主要部分
# ---------------------------------------------------------------------------

_FAILURE_CASES: tuple[tuple[str, str, object], ...] = (
    ("工具名不存在（模型可能编）", "write_file", {"path": "requirements.txt"}),
    ("少了必填参数（模型可能漏）", "read_file", {}),
    (
        "多了 schema 没有的参数（模型可能编）",
        "read_file",
        {"path": "requirements.txt", "encoding": "utf-8"},
    ),
    ("参数类型不对（模型可能给数字）", "read_file", {"path": 123}),
    ("参数根本不是 JSON 对象", "read_file", "requirements.txt"),
    ("文件不存在（T3 要考的就是这个）", "read_file", {"path": "data/nope.txt"}),
    ("路径越界（安全边界）", "read_file", {"path": "../README.md"}),
    ("路径指向目录而不是文件", "read_file", {"path": "data"}),
)


# 只在自检里注册的假工具：它会卡住，用来验超时。
# 它**不进 _TOOL_SPECS**，所以永远不出现在 TOOLS 里 —— 模型看不到它。
def _slow_probe(seconds: str) -> str:
    time.sleep(float(seconds))
    return "睡醒了"


_SLOW_SPEC = ToolSpec(
    name="slow_probe",
    description="自检专用，不会发给模型",
    params={"seconds": "睡多少秒"},
    required=("seconds",),
    func=_slow_probe,
)


def _self_test() -> None:
    seen: list[tuple[str, str]] = []  # (标签, dispatch 的输出) —— 留到最后统一查泄露

    print("=== dispatch 的失败路径自检（全部本地执行，不发 API 请求）===")
    for label, name, args in _FAILURE_CASES:
        out = dispatch(name, args)
        seen.append((label, out))
        # 第 7 步新增：文本之外，结构化字段也必须带上 —— JSONL 靠它聚合
        assert isinstance(out, Observation), f"{label} 没有带上结构化字段"
        assert isinstance(out, str), f"{label} 破坏了'一律返回字符串'"
        print(f"\n[{label}]")
        print(f"  dispatch({name!r}, {args!r})")
        print(f"  -> {out}")
        print(f"     error_kind={out.error_kind!r}  error_type={out.error_type!r}")

    print("\n=== 超时：PLAN §4.1 第 5 条 ===")
    _REGISTRY[_SLOW_SPEC.name] = _SLOW_SPEC  # 只进 dispatch 的注册表，不进 TOOLS
    print("  dispatch('slow_probe', {'seconds': '5'}, timeout=0.5)  ← 只肯等 0.5 秒")
    started = time.perf_counter()
    out = dispatch("slow_probe", {"seconds": "5"}, timeout=0.5)
    elapsed = time.perf_counter() - started
    seen.append(("工具超时", out))
    print(f"  -> {out}")
    print(f"     error_kind={out.error_kind!r}  error_type={out.error_type!r}")
    print(f"  实际等了 {elapsed:.2f} 秒（不是 5 秒 —— 超时生效）")
    print(f"  模型能看到的工具仍然只有：{[s.name for s in _TOOL_SPECS]}")
    del _REGISTRY[_SLOW_SPEC.name]

    print("\n=== 泄露自检：ERROR 文本里不许出现主机路径 ===")
    suspects = (str(ROOT), str(ROOT.parent), chr(92) * 2)  # chr(92) 就是反斜杠
    leaked = [(label, out) for label, out in seen if any(s in out for s in suspects)]
    for label, out in leaked:
        print(f"  [泄露] {label} -> {out}")
    if leaked:
        print(f"  失败：{len(leaked)} 条输出里出现了主机路径")
    else:
        print(f"  OK：{len(seen)} 条输出里没有一条含有主机路径或双反斜杠")

    print("\n=== 正常路径对照 ===")
    out = dispatch("read_file", {"path": "requirements.txt"})
    print("  dispatch('read_file', {'path': 'requirements.txt'}) 前 60 字符：")
    print(f"  -> {out[:60]!r}")
    print("\n注意每一行都以 ERROR 开头且**没有抛出异常** —— 这就是 dispatch 存在的意义。")


if __name__ == "__main__":
    _self_test()
