"""工具层：agent 能做的 7 件事 + `dispatch` + 自检。

三条设计原则（整个项目的骨架都在这三句里）：

1. **`dispatch` 一律返回字符串，绝不向外抛异常。**
   异常、超时、拒绝全部转成文本回喂给模型 —— 只有模型有能力读着错误改策略。
   （判定层不再受这条约束：它要的是结构化结果，见 `eval/suite.py`。）
2. **一个工具只做一件事。** `list_dir` 不返回内容预览，`search` 不返回上下文，
   `read_file` 只读一段。预览看着贴心，实际会把"多步任务"压成一步，
   并且让一个 O(1) 操作变成 O(n)（还受 20 秒超时约束）。
3. **写操作要能被拒绝。** `tests/` 是判据不是实现，改它等于改考卷；
   `.env` / `.git` 是密钥与历史，读它等于开旁路。

自检：

    mini-agent\\.venv\\Scripts\\python.exe swe-agent\\tools.py
"""

from __future__ import annotations

import ast
import fnmatch
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
for _extra in (HERE,):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

from runtime_env import child_env  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# ---------------------------------------------------------------------------
# 上限
# ---------------------------------------------------------------------------
TOOL_TIMEOUT = 20.0  # 单次工具调用（秒）
TEST_TIMEOUT = 60.0  # 跑测试（秒）——比普通工具宽，套件本身可能要几秒
MAX_READ_BYTES = 200_000
MAX_WRITE_BYTES = 200_000
MAX_READ_LINES = 400
MAX_LIST_ENTRIES = 200
SEARCH_MAX_HITS = 50
OUTPUT_TAIL = 4000
_SKIP_DIRS = {".git", "__pycache__", ".venv", ".tmp", "node_modules", "_selftest"}

ROOT: Path = HERE / "sandbox" / "_unset"


def configure(root: str | Path) -> Path:
    """把工具层的"世界"钉到一次运行的仓库副本上。每次运行前必须调用。"""
    global ROOT
    ROOT = Path(root).resolve()
    if not ROOT.is_dir():
        raise ValueError(f"沙箱副本不存在: {ROOT}")
    return ROOT


# ---------------------------------------------------------------------------
# 拒绝与观察
# ---------------------------------------------------------------------------
class Refused(Exception):
    """工具主动拒绝（越界、改测试、读密钥…）。`kind` 会进轨迹，供失败归因。"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


class Observation(str):
    """回给模型的文本。**同时**携带给程序看的字段 —— 两件事别混。"""

    error_kind: str | None
    error_type: str | None

    def __new__(
        cls, text: str, *, error_kind: str | None = None, error_type: str | None = None
    ) -> "Observation":
        obj = super().__new__(cls, text)
        obj.error_kind = error_kind
        obj.error_type = error_type
        return obj

    @property
    def is_error(self) -> bool:
        return self.error_kind is not None


_SENSITIVE_NAMES = {".env", ".git", ".git-credentials", "id_rsa", "id_ed25519"}
_SENSITIVE_SUFFIX = (".key", ".pem", ".p12", ".pfx", ".kdbx")
_SENSITIVE_PREFIX = ("credentials", ".env.")


def _rel(target: Path) -> str:
    return target.relative_to(ROOT).as_posix()


def _resolve(path: str, *, write: bool = False) -> Path:
    """把模型给的路径解析成 ROOT 内的绝对路径；不合法就拒绝。

    三条拒绝在这里一次性做完，因为**这是唯一必经的入口** ——
    只堵 `read_file` 等于没堵（`list_dir` 是第二条通道）。
    """
    if not isinstance(path, str) or not path.strip():
        raise Refused("bad-args", "path 不能为空")
    raw = Path(path)
    target = raw.resolve() if raw.is_absolute() else (ROOT / raw).resolve()
    try:
        rel = target.relative_to(ROOT)
    except ValueError:
        raise Refused("escape", "只允许访问仓库副本内的路径（不允许 .. 或绝对路径跳出）") from None

    parts = [p.lower() for p in rel.parts]
    if parts and parts[0] == "tests" and write:
        raise Refused("tests-write", "tests/ 是这个项目的约定（判据），不允许修改；请改实现")
    if any(p in _SENSITIVE_NAMES for p in parts):
        raise Refused("sensitive", "该路径属于密钥/版本库内部，不提供给模型")
    name = target.name.lower()
    if name in _SENSITIVE_NAMES or name.startswith(_SENSITIVE_PREFIX) and name != ".env.example":
        raise Refused("sensitive", "该路径属于密钥文件，不提供给模型")
    if name.endswith(_SENSITIVE_SUFFIX):
        raise Refused("sensitive", "该路径像私钥，不提供给模型")
    return target


# ---------------------------------------------------------------------------
# 工具 1：list_dir
# ---------------------------------------------------------------------------
def list_dir(path: str = ".") -> str:
    target = _resolve(path)
    if not target.exists():
        raise Refused("not-found", f"路径不存在: {path}")
    if not target.is_dir():
        raise Refused("not-a-dir", f"{path} 不是目录（用 read_file 读文件）")

    try:
        entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError as exc:
        raise Refused("io", f"列目录失败: {exc}") from None

    lines: list[str] = []
    for entry in entries[:MAX_LIST_ENTRIES]:
        try:
            size = entry.stat().st_size
        except OSError:
            size = 0
        lines.append(f"{entry.name}/" if entry.is_dir() else f"{entry.name}  ({size} B)")
    note = ""
    if len(entries) > MAX_LIST_ENTRIES:
        note = f"\n（共 {len(entries)} 项，只列前 {MAX_LIST_ENTRIES} 项）"
    return f"{_rel(target) if target != ROOT else '.'}  共 {len(entries)} 项\n" + "\n".join(lines) + note


# ---------------------------------------------------------------------------
# 工具 2：read_file（分页 + 行号）
# ---------------------------------------------------------------------------
def read_file(path: str, start_line: int = 1, max_lines: int = 200) -> str:
    target = _resolve(path)
    if not target.exists():
        raise Refused("not-found", f"文件不存在: {path}")
    if target.is_dir():
        raise Refused("is-dir", f"{path} 是目录（用 list_dir 看里面有什么）")

    size = target.stat().st_size
    if size > MAX_READ_BYTES:
        raise Refused(
            "too-big",
            f"文件 {size} 字节，超过单次读取上限 {MAX_READ_BYTES}；先用 search 定位行号再读",
        )

    raw = target.read_bytes()
    note = ""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
        note = "  （⚠ 该文件不是合法 UTF-8，已用替换字符解码）"
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    total = len(lines)

    start_line = max(1, int(start_line))
    if total and start_line > total:
        raise Refused("bad-range", f"start_line={start_line} 超过文件总行数 {total}")
    max_lines = max(1, min(int(max_lines), MAX_READ_LINES))
    chunk = lines[start_line - 1 : start_line - 1 + max_lines]
    numbered = [f"{number:>4}| {line}" for number, line in enumerate(chunk, start=start_line)]

    tail = ""
    next_line = start_line + len(chunk)
    if chunk and next_line <= total:
        tail = f"\n（已截断：用 start_line={next_line} 继续）"
    return f"{_rel(target)}  共 {total} 行{note}\n" + "\n".join(numbered) + tail


# ---------------------------------------------------------------------------
# 工具 3：search
# ---------------------------------------------------------------------------
def _match_glob(rel: str, glob: str) -> bool:
    if "/" in glob or "\\" in glob:
        return fnmatch.fnmatch(rel, glob.replace("\\", "/"))
    return fnmatch.fnmatch(Path(rel).name, glob)


def _is_sensitive_file(path: Path) -> bool:
    name = path.name.lower()
    return (
        name in _SENSITIVE_NAMES
        or (name.startswith(_SENSITIVE_PREFIX) and name != ".env.example")
        or name.endswith(_SENSITIVE_SUFFIX)
    )


def _walk_files(base: Path):
    for current, dirs, files in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS and not d.startswith("."))
        for name in sorted(files):
            path = Path(current) / name
            # 密钥文件连"被搜索到"都不行 —— 否则 search 成了绕过 read_file 的旁路
            if _is_sensitive_file(path):
                continue
            yield path


def search(pattern: str, glob: str = "*.py", max_hits: int = SEARCH_MAX_HITS) -> str:
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        raise Refused("bad-regex", f"正则语法错误: {exc}（pattern 用的是 Python re 语法）") from None

    max_hits = max(1, min(int(max_hits), 200))
    hits: list[str] = []
    scanned = 0
    for path in _walk_files(ROOT):
        rel = _rel(path)
        if not _match_glob(rel, glob):
            continue
        scanned += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "\x00" in text[:2000]:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                hits.append(f"{rel}:{number}: {line.strip()[:200]}")
                if len(hits) >= max_hits:
                    break
        if len(hits) >= max_hits:
            break

    if not hits:
        return f"没有命中（pattern={pattern!r} glob={glob!r}，扫了 {scanned} 个文件）"
    note = f"\n（已达上限 {max_hits} 处，收窄 pattern 或 glob 再看）" if len(hits) >= max_hits else ""
    return f"命中 {len(hits)} 处（glob={glob!r}，扫了 {scanned} 个文件）\n" + "\n".join(hits) + note


# ---------------------------------------------------------------------------
# 工具 4：outline（ast 符号表）
# ---------------------------------------------------------------------------
def outline(path: str) -> str:
    target = _resolve(path)
    if not target.is_file():
        raise Refused("not-found", f"文件不存在: {path}")
    note = ""
    try:
        source = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        source = target.read_text(encoding="utf-8", errors="replace")
        note = "（⚠ 不是合法 UTF-8，已替换解码）"
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise Refused(
            "syntax", f"语法错误：第 {exc.lineno} 行 {exc.msg}（这个文件现在跑不起来）"
        ) from None

    rows: list[str] = []

    def walk(node: ast.AST, depth: int) -> None:
        for child in node.body:  # type: ignore[attr-defined]
            pad = "  " * depth
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                doc = ast.get_docstring(child) or ""
                first = doc.strip().splitlines()[0] if doc.strip() else ""
                rows.append(f"{pad}def {child.name}(L{child.lineno})" + (f" — {first}" if first else ""))
                walk(child, depth + 1)
            elif isinstance(child, ast.ClassDef):
                bases = ", ".join(ast.unparse(b) for b in child.bases)
                rows.append(
                    f"{pad}class {child.name}(L{child.lineno})" + (f" ({bases})" if bases else "")
                )
                walk(child, depth + 1)

    walk(tree, 0)
    head = f"{_rel(target)}  共 {len(rows)} 个定义{note}"
    if not rows:
        return head + "\n（这个文件里没有函数或类定义）"
    note2 = "\n（已截断）" if len(rows) > 200 else ""
    return head + "\n" + "\n".join(rows[:200]) + note2


# ---------------------------------------------------------------------------
# 工具 5 / 6：apply_edit、write_file（唯一的写通道）
# ---------------------------------------------------------------------------
def _syntax_glance(target: Path) -> str:
    """写完顺手做一次语法检查：改坏了立刻告诉模型，别让它花 3 步才发现。"""
    if target.suffix != ".py":
        return ""
    try:
        ast.parse(target.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError as exc:
        return f"\n⚠ 语法错误：第 {exc.lineno} 行 {exc.msg}（文件已写入，请修正）"
    return "\n（语法检查通过）"


def _read_for_write(target: Path) -> str:
    if not target.is_file():
        raise Refused("not-found", f"文件不存在: {_rel(target)}")
    if target.stat().st_size > MAX_READ_BYTES:
        raise Refused("too-big", f"文件超过 {MAX_READ_BYTES} 字节，不能整段改写")
    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise Refused("encoding", "该文件不是 UTF-8，拒绝改写（避免把编码搞混）") from None
    return text.replace("\r\n", "\n").replace("\r", "\n")


def apply_edit(path: str, old: str, new: str) -> str:
    if not isinstance(old, str) or not isinstance(new, str):
        raise Refused("bad-args", "old / new 必须是字符串")
    old = old.replace("\r\n", "\n").replace("\r", "\n")
    new = new.replace("\r\n", "\n").replace("\r", "\n")
    if not old.strip():
        raise Refused("bad-args", "old 不能为空")
    if old == new:
        raise Refused("bad-args", "old 与 new 相同，这次调用不会产生任何改动")

    target = _resolve(path, write=True)
    text = _read_for_write(target)
    count = text.count(old)
    if count == 0:
        raise Refused(
            "no-match",
            "未找到要替换的原文。old 必须与文件内容**逐字符一致**（含缩进与换行）——"
            "先 read_file 把要改的那几行原样抄过来。",
        )
    if count > 1:
        raise Refused(
            "ambiguous",
            f"原文在文件里出现了 {count} 次，不唯一。请在 old 里补足上下文，让它可以只匹配一处。",
        )

    updated = text.replace(old, new, 1)
    target.write_text(updated, encoding="utf-8", newline="\n")
    delta = updated.count("\n") - text.count("\n")
    return f"已修改 {_rel(target)}：行数 {text.count(chr(10))}→{updated.count(chr(10))}（{delta:+d}）" + _syntax_glance(target)


def write_file(path: str, content: str) -> str:
    if not isinstance(content, str):
        raise Refused("bad-args", "content 必须是字符串")
    target = _resolve(path, write=True)
    if target.exists():
        raise Refused(
            "exists", f"{path} 已存在。改已有文件请用 apply_edit（write_file 只用于新建）"
        )
    payload = content.replace("\r\n", "\n").replace("\r", "\n")
    if len(payload.encode("utf-8")) > MAX_WRITE_BYTES:
        raise Refused("too-big", f"内容超过 {MAX_WRITE_BYTES} 字节上限")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload, encoding="utf-8", newline="\n")
    except OSError as exc:
        raise Refused("io", f"创建失败: {exc}") from None
    return f"已创建 {_rel(target)}（{len(payload)} 字符）" + _syntax_glance(target)


# ---------------------------------------------------------------------------
# 工具 7：run_tests（唯一执行通道）
# ---------------------------------------------------------------------------
_TARGET_RE = re.compile(r"^[A-Za-z_][\w.]*$")
_FAIL_LINE = re.compile(r"^(FAIL|ERROR): (.*)$", re.M)


def run_suite_raw(target: str = "") -> tuple[int, str]:
    """跑一次测试，返回 `(退出码, 原始输出)`；超时与启动失败记成 -1。

    拆出来是为了让**判定侧**（loop 收尾时的自校验）能拿到退出码，
    而不必去正则解析给模型看的那段格式化文本。模型看到的仍然是 `run_tests`。
    """
    args = [sys.executable, "-m", "unittest"]
    if target:
        if not _TARGET_RE.fullmatch(target):
            raise Refused(
                "bad-args",
                "target 只能是 模块 / 模块.类 / 模块.类.方法（例如 tests.test_store 或"
                " tests.test_store.TestNoteStore.test_add_and_get）",
            )
        args.append(target)
    else:
        args += ["discover", "-s", "tests", "-t", "."]

    try:
        proc = subprocess.run(
            args,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TEST_TIMEOUT,
            env=child_env(),
        )
    except subprocess.TimeoutExpired:
        raise Refused(
            "test-timeout", f"测试超时（{TEST_TIMEOUT:.0f} 秒未跑完）——可能有死循环或长时间等待"
        ) from None
    except OSError as exc:
        raise Refused("io", f"无法启动测试进程: {exc}") from None
    out = ((proc.stdout or "") + (proc.stderr or "")).replace("\r\n", "\n")
    return proc.returncode, out


def run_tests(target: str = "") -> str:
    started = time.perf_counter()
    code, out = run_suite_raw(target)
    elapsed = time.perf_counter() - started
    ran = re.search(r"^Ran (\d+) test", out, re.M)
    bad = [f"{kind}: {name}".strip() for kind, name in _FAIL_LINE.findall(out)]

    lines = [f"退出码 {code}（{elapsed:.1f}s）"]
    if ran:
        lines.append(f"Ran {ran.group(1)} test(s)")
    if bad:
        lines.append(f"未通过 {len(bad)} 条：")
        lines.extend(f"  {item}" for item in bad[:8])
        if len(bad) > 8:
            lines.append(f"  ……另有 {len(bad) - 8} 条")
    elif code == 0:
        lines.append("全部通过")

    if code != 0 and (not ran or "_FailedTest" in out):
        lines.append(
            "ERROR: 套件没能启动（有模块导入失败）——先修导入错误，别怀疑测试内容"
        )

    tail = out[-OUTPUT_TAIL:]
    if len(out) > OUTPUT_TAIL:
        lines.append(f"末尾输出（只保留最后 {OUTPUT_TAIL} 字符）：")
    else:
        lines.append("输出：")
    lines.append(tail.rstrip())
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 工具清单（JSON Schema，直接交给模型）
# ---------------------------------------------------------------------------
def _schema(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


_P_STR = {"type": "string"}
_P_INT = {"type": "integer"}

_TOOL_SPECS: list[dict] = [
    _schema(
        "list_dir",
        "列出一个目录的直接条目（含大小）。只列名字与大小，不返回文件内容；"
        "想读内容用 read_file，想按内容找文件用 search。",
        {"path": {**_P_STR, "description": "相对仓库根目录的路径，默认 '.'"}},
        [],
    ),
    _schema(
        "read_file",
        "读一个文本文件的一部分，返回带行号的内容（行号可直接用于 apply_edit 的定位）。"
        "单次最多 200 行，被截断时会提示用 start_line 继续。",
        {
            "path": {**_P_STR, "description": "相对仓库根目录的文件路径"},
            "start_line": {**_P_INT, "description": "从第几行开始（1 起），默认 1"},
            "max_lines": {**_P_INT, "description": "最多读多少行，默认 200，上限 400"},
        },
        ["path"],
    ),
    _schema(
        "search",
        "用正则在整个仓库里按行搜索，返回 `相对路径:行号: 该行内容`。"
        "定位函数、报错信息、字段名时先用它，不要靠猜文件名。",
        {
            "pattern": {**_P_STR, "description": "Python re 语法的正则，例如 'def parse_duration'"},
            "glob": {**_P_STR, "description": "只搜哪些文件，默认 '*.py'（也支持 'notes/*.py'）"},
            "max_hits": {**_P_INT, "description": "最多返回多少处，默认 50"},
        },
        ["pattern"],
    ),
    _schema(
        "outline",
        "用 ast 列出一个 Python 文件的类与函数（带行号和 docstring 首行）。"
        "比整文件读一遍更省 token；文件有语法错误时会直接指出行号。",
        {"path": {**_P_STR, "description": "相对仓库根目录的 .py 文件路径"}},
        ["path"],
    ),
    _schema(
        "apply_edit",
        "把文件里**恰好出现一次**的一段原文替换成新内容。"
        "old 必须与文件内容逐字符一致（含缩进与换行）；出现 0 次或多次都会被拒绝并说明原因。"
        "改已有文件一律用它，不要用 write_file。",
        {
            "path": {**_P_STR, "description": "相对仓库根目录的文件路径"},
            "old": {**_P_STR, "description": "要被替换的原文（必须唯一匹配）"},
            "new": {**_P_STR, "description": "替换后的内容"},
        },
        ["path", "old", "new"],
    ),
    _schema(
        "write_file",
        "新建一个文件（仅新建，目标已存在会被拒绝）。改动现有文件请用 apply_edit。",
        {
            "path": {**_P_STR, "description": "相对仓库根目录的新文件路径"},
            "content": {**_P_STR, "description": "文件内容"},
        },
        ["path", "content"],
    ),
    _schema(
        "run_tests",
        "运行项目的测试套件，返回退出码、用例数、未通过的用例与输出末尾。"
        "不传 target 就跑全部；传 target 只跑一部分（模块 / 模块.类 / 模块.类.方法），"
        "迭代时先跑单个用例能省时间和 token。",
        {"target": {**_P_STR, "description": "可选：tests.test_store.TestNoteStore.test_add_and_get"}},
        [],
    ),
]

TOOLS: list[dict] = list(_TOOL_SPECS)

_HANDLERS: dict[str, Callable[..., str]] = {
    "list_dir": list_dir,
    "read_file": read_file,
    "search": search,
    "outline": outline,
    "apply_edit": apply_edit,
    "write_file": write_file,
    "run_tests": run_tests,
}

_SPEC_BY_NAME = {spec["function"]["name"]: spec["function"] for spec in _TOOL_SPECS}


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------
def _run_with_timeout(name: str, fn: Callable[..., str], args: dict, timeout: float) -> Observation:
    box: dict[str, Observation] = {}

    def work() -> None:
        try:
            value = fn(**args)
            box["value"] = Observation(value)
        except Refused as exc:
            box["value"] = Observation(f"ERROR: {exc}", error_kind=exc.kind, error_type="Refused")
        except Exception as exc:  # noqa: BLE001 —— 工具边界绝不外抛
            box["value"] = Observation(
                f"ERROR: {type(exc).__name__}: {exc}",
                error_kind="tool-error",
                error_type=type(exc).__name__,
            )

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        # 超时后那个守卫线程仍在跑，可能继续写文件 —— 这是已知代价：
        # 想强行中断一个正在写盘的线程，在 Python 里没有安全做法。工具都很快，
        # 会撞上的只有外部进程（run_tests 自己有 subprocess 超时，轮不到这里）。
        return Observation(
            f"ERROR: 工具 {name} 超时（{timeout:.0f} 秒未返回）", error_kind="timeout"
        )
    return box.get("value") or Observation("ERROR: 工具没有返回值", error_kind="internal")


def _check_args(name: str, args: dict) -> str | None:
    """返回错误文本；参数没问题返回 None。

    **不自己拼报错**：缺什么参数直接说缺哪个，比让 TypeError 冒出来清楚得多，
    而模型看的就是这段文本。
    """
    spec = _SPEC_BY_NAME.get(name)
    if spec is None:
        return f"ERROR: 未知工具 {name!r}；可用的是 {sorted(_HANDLERS)}"
    params = spec["parameters"]
    required = params.get("required", [])
    missing = [key for key in required if key not in args]
    if missing:
        return (
            f"ERROR: 缺少参数 {', '.join(missing)}（{name} 需要 "
            f"{', '.join(params['properties'])}）"
        )
    unknown = [key for key in args if key not in params["properties"]]
    if unknown:
        return (
            f"ERROR: 不认识的参数 {', '.join(unknown)}（{name} 支持 "
            f"{', '.join(params['properties'])}）"
        )
    return None


def dispatch(name: str, args: dict, timeout: float = TOOL_TIMEOUT) -> Observation:
    """执行一个工具调用。**永远返回 Observation（str 的子类），永远不抛异常。**"""
    if not isinstance(args, dict):
        return Observation("ERROR: 工具参数必须是 JSON 对象", error_kind="bad-args")
    problem = _check_args(name, args)
    if problem:
        return Observation(problem, error_kind="bad-args")
    return _run_with_timeout(name, _HANDLERS[name], args, timeout)


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------
_SELFTEST_ROOT = HERE / "sandbox" / "_selftest"


def _make_repo(base: Path, *, broken: bool = False) -> Path:
    from fsutil import rmtree_force

    rmtree_force(base)
    (base / "pkg").mkdir(parents=True)
    (base / "pkg" / "__init__.py").write_text("", newline="\n")
    (base / "pkg" / "core.py").write_text(
        '"""示例模块。"""\n\n\ndef add(a, b):\n    """两数相加。"""\n    return a + b\n\n\n'
        "class Box:\n    def put(self, value):\n        return value\n",
        encoding="utf-8",
        newline="\n",
    )
    (base / "tests").mkdir()
    (base / "tests" / "__init__.py").write_text("", newline="\n")
    body = "import unittest\nfrom pkg.core import add\n\n\nclass TestAdd(unittest.TestCase):\n"
    if broken:
        body = "import unittest\nimport 不存在的模块\n"
    else:
        body += (
            "    def test_pair(self):\n        self.assertEqual(add(1, 2), 3)\n\n"
            "    def test_solo(self):\n        self.assertEqual(add(2, 2), 5)\n"
        )
    (base / "tests" / "test_core.py").write_text(body, encoding="utf-8", newline="\n")
    (base / "README.md").write_text("# demo\n", encoding="utf-8", newline="\n")
    (base / ".env").write_text("OPENAI_API_KEY=secret\n", encoding="utf-8", newline="\n")
    return base


def _self_test() -> int:
    """26 条断言：每个工具的正常路径与每条拒绝都要被真实触发一次。

    "没走到的分支等于没测过" —— 所以这里不测"函数存在"，只测"调用后看到什么"。
    """
    results: list[tuple[bool, str, str]] = []

    def expect(name: str, ok: bool, detail: str = "") -> None:
        results.append((ok, name, detail))

    base = _SELFTEST_ROOT / "repo"
    _make_repo(base)
    configure(base)

    # 1-2 读文件
    text = read_file("pkg/core.py")
    expect("read_file 带行号", "   4| def add(a, b):" in text and "   9| class Box:" in text, text[:40])
    expect("read_file 分页提示", "start_line=3" in read_file("pkg/core.py", start_line=1, max_lines=2))
    expect(
        "read_file 越界行号被拒",
        isinstance(dispatch("read_file", {"path": "pkg/core.py", "start_line": 999}).error_kind, str),
    )

    # 3-5 三类拒绝
    expect(
        "拒绝跳出仓库",
        dispatch("read_file", {"path": "../seeds.py"}).error_kind == "escape",
    )
    expect(
        "拒绝读密钥",
        dispatch("read_file", {"path": ".env"}).error_kind == "sensitive",
    )
    expect(
        "拒绝绝对路径",
        dispatch("read_file", {"path": str(HERE / "tools.py")}).error_kind == "escape",
    )
    expect("读目录被拒", dispatch("read_file", {"path": "pkg"}).error_kind == "is-dir")
    expect("读不存在文件", dispatch("read_file", {"path": "pkg/nope.py"}).error_kind == "not-found")

    # 6 list_dir
    listing = list_dir(".")
    expect("list_dir 目录带斜杠", "pkg/" in listing and "tests/" in listing, listing[:40])

    # 7-8 search
    hits = search(r"def \w+\(")
    expect("search 返回 路径:行: 内容", "pkg/core.py:4: def add(a, b):" in hits, hits[:60])
    expect("search 正则错误被拒", dispatch("search", {"pattern": "("}).error_kind == "bad-regex")
    expect("search 无命中", "没有命中" in search(r"绝不可能出现的字符串_xyz"))

    # 9-10 outline
    symbols = outline("pkg/core.py")
    expect("outline 出 def/class 与行号", "def add(L4)" in symbols and "class Box(L9)" in symbols, symbols[:60])
    # 注意：不能断言「返回值里不含 OPENAI_API_KEY」—— 无命中时回显里就带着 pattern 本身。
    secret_hit = search("OPENAI_API_KEY", glob="*")
    expect(
        "search 不返回密钥文件",
        "没有命中" in secret_hit,
        secret_hit[:80],
    )
    (base / "broken.py").write_text("def f(:\n", encoding="utf-8", newline="\n")
    bad_outline = dispatch("outline", {"path": "broken.py"})
    expect("outline 抓到语法错误", bad_outline.error_kind == "syntax" and "第 1 行" in str(bad_outline))

    # 11-14 apply_edit
    expect(
        "apply_edit 零匹配被拒",
        dispatch("apply_edit", {"path": "pkg/core.py", "old": "不存在的内容", "new": "x"}).error_kind
        == "no-match",
    )
    (base / "dup.py").write_text("x = 1\nx = 1\n", encoding="utf-8", newline="\n")
    expect(
        "apply_edit 多匹配被拒",
        dispatch("apply_edit", {"path": "dup.py", "old": "x = 1", "new": "x = 2"}).error_kind
        == "ambiguous",
    )
    edited = apply_edit("pkg/core.py", "    return a + b", "    return a + b + 0")
    raw = (base / "pkg" / "core.py").read_bytes()
    expect("apply_edit 成功且只写 LF", "已修改 pkg/core.py" in edited and b"\r\n" not in raw, edited[:40])
    expect("apply_edit 改测试被拒", dispatch(
        "apply_edit", {"path": "tests/test_core.py", "old": "3", "new": "4"}
    ).error_kind == "tests-write")

    # 15-17 write_file
    expect("write_file 新建成功", "已创建 notes.md" in write_file("notes.md", "# 笔记\n"))
    expect(
        "write_file 拒绝覆盖",
        dispatch("write_file", {"path": "notes.md", "content": "x"}).error_kind == "exists",
    )
    expect(
        "write_file 拒绝写 tests/",
        dispatch("write_file", {"path": "tests/new_test.py", "content": "x"}).error_kind
        == "tests-write",
    )
    expect(
        "写坏 python 会提示语法错误",
        "语法错误" in write_file("bad_syntax.py", "def f(:\n"),
    )

    # 18-21 run_tests
    failing = run_tests()
    expect(
        "run_tests 报出失败用例",
        "退出码 1" in failing and "Ran 2 test(s)" in failing and "FAIL: test_solo" in failing,
        failing.splitlines()[0] if failing else "",
    )
    passing = run_tests("tests.test_core.TestAdd.test_pair")
    expect("run_tests 单用例通过", "退出码 0" in passing and "全部通过" in passing, passing[:40])
    expect(
        "run_tests target 非法被拒",
        dispatch("run_tests", {"target": "--help"}).error_kind == "bad-args",
    )
    _make_repo(_SELFTEST_ROOT / "broken_repo", broken=True)
    configure(_SELFTEST_ROOT / "broken_repo")
    expect("run_tests 套件没启动会明说", "套件没能启动" in run_tests())

    # 22 dispatch 的边界（未知工具 / 缺参数 / 多参数 / 参数类型）
    expect("未知工具给可用清单", "未知工具" in dispatch("nope", {}), "")
    expect("缺参数明说缺哪个", "缺少参数 path" in dispatch("read_file", {}), "")
    expect("多参数被拒", "不认识的参数" in dispatch("read_file", {"path": "x", "oops": 1}), "")
    expect(
        "参数类型错也不外抛",
        isinstance(dispatch("read_file", {"path": "pkg/core.py", "start_line": "abc"}), str),
    )
    expect("args 不是对象也不外抛", dispatch("read_file", ["bad"]).error_kind == "bad-args")

    # 23 超时：直接打 `_run_with_timeout`，用一个确定会睡过头的假工具
    slow = _run_with_timeout("slow", lambda: time.sleep(0.5) or "x", {}, 0.01)
    expect(
        "超时转成文本（不抛异常）",
        slow.error_kind == "timeout" and "超时" in slow,
        f"kind={slow.error_kind}",
    )

    # 24 全部结果都是 str
    expect("dispatch 全返回 str", all(isinstance(dispatch("list_dir", {"path": "."}), str) for _ in range(2)))

    from fsutil import rmtree_force

    rmtree_force(_SELFTEST_ROOT)

    passed = sum(1 for ok, _, _ in results if ok)
    for ok, name, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f"   → {detail}"))
    print(f"\n自检 {passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(_self_test())
