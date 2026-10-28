"""阶段 0 · 第 3 / 5 / 7 / 9 步：工具注册表 + dispatch —— 把「模型的请求」变成「一段文本观察」。

第 2 步我们只让模型"说出"它想调什么，没有执行；第 3 步补上执行。
（第 2 步那个只打印 tool_calls 的探针已被 agent.py 完整取代，故移除。）
第 5 步改的是**错误文本长什么样**；第 7 步给失败文本搭上结构化字段（`Observation`）；
第 9 步加**第一个有副作用的工具**，并顺手补上只读侧早就存在的一个安全洞；
第 10 步补齐 PLAN §4.3 要求的第 4 个工具 `calculator`（`ast` 白名单，绝不用 `eval`）。

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

第 9 步补的两条：

  6. **"超时"在只读工具和写工具上不是一回事。**
     只读工具超时 = 明确失败（最坏白读一遍）。
     写工具超时 = **结果未知**（可能已经写成功了）。前者可以让模型换条路，
     后者必须叫它先去确认、别盲目重试。这个分岔由 `ToolSpec.side_effect`
     驱动 —— 声明式的字段，不靠人记得在每个地方写 if。

  7. **只读工具也会出事：安全边界必须放在路径解析这一个入口。**
     设计写工具时回头查读侧，发现 `read_file(".env")` 一直能读到密钥
     （实测 929 字符，含真 key），而它会作为观察文本发回远端服务商。
     `list_dir` 隐藏点文件，但 `read_file` 没查 —— 于是把黑名单放进 `_resolve`，
     **所有**工具自动被保护，将来加第 5 个工具也不用有人记得补。
     和第 3 步那条"只能有一个数据源"是同一种思路。

阶段 1 第 2 步补的一条：

  8. **第三类拒绝：路径合法、内容不敏感，但"给了就作弊"。**
     `logs/run-*.jsonl` 是运行轨迹 —— 上一次运行、甚至本次运行正在写的那份。
     它是**外部历史**：让本次运行读到任何历史，"每次运行都从同一起点开始"
     就不成立了，而这正是评测（重复测量）的地基。所以它不该进模型上下文。

     和前两类拒绝**理由不同、后果不同**，因此单独一个异常类：
       * `PathEscape`    —— 路径**不合法**（越出 mini-agent 目录）→ 位置问题
       * `SensitivePath` —— 路径合法，内容是**凭据** → 安全问题
       * `RunTrace`      —— 路径合法、内容也不敏感（就是一段对话记录）
                            → **测量有效性问题**

     而这一次有两处必须同时改：`_resolve`（挡 `read_file` / `append_file`）
     **和** `list_dir`（挡"看目录名就推出答案"）。只改 `_resolve` 等于没改 ——
     因为 `list_dir("logs")` 列出的 `run-20261012-202119.jsonl` 之类，
     本身就长得跟 T1/T4 的答案 `run-<时间戳>.jsonl` 一模一样。

运行（纯本地，不发 API 请求）：
    mini-agent\\.venv\\Scripts\\python.exe mini-agent\\tools.py
"""

from __future__ import annotations

import ast
import math
import operator
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# 工具层**允许**依赖日志层，方向只有这一个。
#
# 因为"什么算一份轨迹"这个知识属于 runlog.py（它才是造轨迹的那个），
# 在 tools.py 里手写一份 "run-*.jsonl" 就是第 3 步铁律 3 说的"两个数据源" ——
# 两边不一致时不会报错，只会静默地不再拦截。
from runlog import LOGS_DIR, TRACE_GLOB

# Windows 上 stdout 被重定向时 Python 会退回 GBK，上游按 UTF-8 解码会乱码
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 工具路径的基准目录 = mini-agent 目录。
# 这样 PLAN §4.4 的任务文本（"读取 data/handbook.txt"）可以原样使用。
ROOT = Path(__file__).resolve().parent
MAX_CHARS = 2000
MAX_ENTRIES = 200  # list_dir 一次最多列多少条（防止把输出冲爆）
MAX_APPEND = 2000  # append_file 单次写入的字符上限

# calculator 的三道闸。**这个工具没有 I/O，唯一的风险是被算爆。**
CALC_MAX_CHARS = 200  # 表达式长度上限
CALC_MAX_ABS = 10**15  # 每一步结果的绝对值上限
CALC_MAX_POW = 1000  # ** 的指数上限

# ---------------------------------------------------------------------------
# 敏感文件：**所有**工具都碰不到它们
#
# 为什么在第 9 步才补：设计写工具时回头查了一下读侧，发现
# `read_file(".env")` 一直能读到密钥（实测 929 字符，里面有真 key）。
# list_dir 隐藏了点文件，但 read_file 没查 —— 于是模型可以把密钥读出来，
# 作为 tool 观察文本随下一轮请求发回远端。**只读工具也会出事。**
#
# 规则故意写得简单甚至略宽（`.env.example` 这个无害模板也一起挡了）：
# 一条能被人一眼看懂、一次说清的规则，比一条聪明但需要解释的规则可靠。
# ---------------------------------------------------------------------------
_SENSITIVE_NAMES = (".env",)
_SENSITIVE_GLOBS = ("*.key", "*.pem", "credentials*")


class PathEscape(PermissionError):
    """路径越出了 mini-agent 目录。

    单独一个类，是为了让 _describe_failure 能一眼认出它 —— 它和操作系统抛的
    PermissionError 必须区别对待：我们的消息是我们自己写的，不含主机路径；
    操作系统的消息里带着绝对路径，绝不能透传给模型。
    """


class SensitivePath(PermissionError):
    """路径落在敏感文件上（凭据等）。消息是我们自己写的，可以安全透传。"""


class RunTrace(PermissionError):
    """路径落在一次运行的轨迹上（`logs/run-*.jsonl`）。

    第三个拒绝类，理由和前两个都不同：

      * `PathEscape`    —— 路径**不合法**（位置问题）
      * `SensitivePath` —— 路径合法，内容是**凭据**（安全问题）
      * `RunTrace`      —— 路径合法、内容也不敏感（就是一段对话记录），
                           但给了它，这次评测就**不算数了**（测量有效性问题）

    实测依据（阶段 0 第 9 步，`run-20261012-214245`）：那次运行的模型读了
    4 份轨迹，其中一份是**它自己正在写的**，还把 T2 的完整题面带进了上下文。
    """


class ToolRefused(Exception):
    """工具**自己**拒绝了这次调用，理由由工具写在消息里。

    这是给"我们亲手抛出的、不含主机路径的"异常留的通用通道。
    除了它和上面几个自建类，任何异常的原始 message 都不得透传给模型。
    """


def _is_sensitive(target: Path) -> bool:
    name = target.name.lower()
    if name in _SENSITIVE_NAMES or name.startswith(".env."):
        return True
    return any(target.match(pattern) for pattern in _SENSITIVE_GLOBS)


def _is_run_trace(target: Path) -> bool:
    """target 是不是一份**运行轨迹**。

    和 `_is_sensitive` 并列，但**不是它的一个分支** —— 判据完全不同：

      * `_is_sensitive` 问的是"这个内容能不能给模型"（凭据不能）
      * `_is_run_trace` 问的是"这个文件是不是测量仪器"（仪器不能进被测系统）

    一个文件可以既不敏感又不是仪器（`data/handbook.txt`），也可以敏感而不
    是仪器（`.env`）。两个问题，两个函数。

    `target.parent == LOGS_DIR` 这个限定不是多余的：`run-*.jsonl` 如果在
    `data/` 或别处，它就不是轨迹，挡它没有理由，还会让人误以为这条规则在
    防一件它并不防的事。（`_SENSITIVE_GLOBS` 反过来是**故意**不限目录的：
    `*.key` 放在哪都不该给模型。）
    """
    return target.parent == LOGS_DIR and target.match(TRACE_GLOB)


def _resolve(path: str) -> Path:
    """把工具收到的相对路径解析成绝对路径，并做三道拒绝。

    三道边界放在**这一层**，而不是每个工具各写一遍 —— 这样将来加第 5、第 6 个
    工具时，它们自动被保护，不需要有人记得去补。这是第 3 步那条
    "只能有一个数据源"的同一种思路：**安全边界也只能有一个入口。**
    """
    target = (ROOT / path).resolve()
    if target != ROOT and ROOT not in target.parents:
        raise PathEscape(f"路径越界：{path} 不在 mini-agent 目录内")
    if _is_sensitive(target):
        raise SensitivePath(f"{path} 属于凭据类敏感文件，不提供给模型")
    if _is_run_trace(target):
        raise RunTrace(f"{path} 是运行轨迹，不提供给模型")
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


def list_dir(path: str) -> str:
    """列出 mini-agent 目录内某个目录的条目：类型 + 字节数 + 名字。

    ⚠️ 故意**不返回任何文件内容**。三个理由，第 1 条是决定性的：

      1. **内容摘要会泄露答案。** T2 要考的是"先列目录、再读对的文件"。
         如果摘要里带了金额，模型看完 list_dir 就能直接回答，**根本不用调
         read_file**。于是 T2 从多步退化成一步，判定显示"通过"，
         但"读对的文件"这一步压根没发生 —— 通过率虚高，比失败更危险。
      2. **代价会失控。** 带摘要就得把目录里每个文件都读一遍：一个 O(1) 的
         操作变成 O(n)，而它的时间预算只有 20 秒（PLAN §4.1）。
         列 100 个文件的目录 = 读 100 个文件。
      3. **一个工具只做一件事。** `ls -l` 给元信息，`head` 给内容；
         想看内容，那有 read_file，而且它有自己的一套边界（截断、越界检查）。

    想看"文件里写了什么"，是 read_file 的职责。这个分工不能糊。

    ⚠️ 另一条规则：**轨迹文件不列。** 见下面过滤那一行。
    """
    target = _resolve(path)
    # 先分清"不存在"和"不是目录"：笼统报错会让模型改错方向
    if not target.exists():
        raise FileNotFoundError(f"{path} 不存在")
    if not target.is_dir():
        raise NotADirectoryError(f"{path} 不是目录")

    # 隐藏项不列：`.env` 里是密钥（连文件名都不给它看）；`.venv` 会把输出冲爆。
    # 轨迹也不列 —— 和"隐藏"同一个道理，但理由不同（见 _is_run_trace）：
    # `list_dir("logs")` 一旦列出 `run-20261012-202119.jsonl`，模型光看目录名
    # 就能拼出 T1/T4 的答案 `run-<时间戳>.jsonl`，题目从"读文件找那一行"
    # 退化成一步。判定会显示"通过"，但被测的那件事压根没发生。
    #
    # 副作用：`logs/` 会显示成空目录。**这不是撒谎** —— 全新 clone 出来的仓库里
    # `logs/` 只有 `.gitkeep`（隐藏项，本来就不列），看起来正好就是空的。
    # 这正是 `.gitkeep` 存在的理由：git 不跟踪空目录，不占位 logs/ 就会消失。
    entries = sorted(
        (p for p in target.iterdir() if not p.name.startswith(".") and not _is_run_trace(p)),
        key=lambda p: (not p.is_dir(), p.name),
    )
    if not entries:
        return f"{path} 里没有可见条目（空目录，或只有隐藏项）"

    lines = []
    for entry in entries[:MAX_ENTRIES]:
        if entry.is_dir():
            lines.append(f"{'dir':<4} {'-':>8}  {entry.name}/")
        else:
            lines.append(f"{'file':<4} {entry.stat().st_size:>8}  {entry.name}")
    if len(entries) > MAX_ENTRIES:
        lines.append(f"...（共 {len(entries)} 个条目，只列了前 {MAX_ENTRIES} 个）")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# calculator：唯一一个"没有 I/O，却依然危险"的工具
#
# PLAN §4.3 的硬要求是「用 ast 白名单，**绝不用 eval()**」。为什么这不只是洁癖：
#
#   eval("__import__('os').system('...')")   ← 一行就能执行任意命令
#
# 而它的风险面跟另外三个工具**完全不同**：read_file/list_dir/append_file 的危险
# 是"碰到不该碰的路径"，靠 _resolve 就挡住了；calculator 不碰任何路径，
# 它的危险是**被算爆** —— 一个 200 字符的表达式可以让 CPU 或内存跑满。
# 所以它需要一整套自己的闸门（见下面三道 CALC_* 常量）。
# ---------------------------------------------------------------------------

# 白名单：只有这些节点类型是允许的。用 **精确类型** 匹配，不用 isinstance ——
# isinstance 会把子类放进来，而 ast 的节点继承关系不是为安全设计的。
_ALLOWED_NODES = frozenset({
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Constant,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.UAdd, ast.USub,
})

_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}


def _guard(value: object) -> int | float:
    """每一步运算之后的检查。任何不合格的结果当场拒绝。"""
    # bool 是 int 的子类，必须排在前面单独挡掉：True + True 不该等于 2
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolRefused(f"结果不是实数（{type(value).__name__}），拒绝返回")
    if isinstance(value, float) and not math.isfinite(value):
        raise ToolRefused("结果是 NaN 或无穷大，拒绝返回")
    if abs(value) > CALC_MAX_ABS:
        raise ToolRefused(f"数值超出上限 {CALC_MAX_ABS}，拒绝继续计算")
    return value


def _eval_node(node: ast.AST) -> int | float:
    """在已经通过白名单的语法树上递归求值。

    `isinstance` 在这里是安全的 —— 因为**白名单已经先跑过一遍**，
    进到这个函数里的节点类型一定是允许的那些。这就是"先拒绝，再求值"：
    不是边算边祈祷。
    """
    if isinstance(node, ast.Constant):
        return _guard(node.value)
    if isinstance(node, ast.UnaryOp):
        operand = _eval_node(node.operand)
        return _guard(-operand if isinstance(node.op, ast.USub) else +operand)
    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > CALC_MAX_POW:
            raise ToolRefused(f"指数 {right} 超过上限 {CALC_MAX_POW}，拒绝计算")
        return _guard(_BINOPS[type(node.op)](left, right))
    raise ToolRefused(f"不支持 {type(node).__name__}")  # 白名单过了就不该到这里


def _format(value: int | float) -> str:
    """4/2 显示成 2 而不是 2.0 —— 模型和人都不需要那个小数点。"""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return f"{value:g}" if isinstance(value, float) else str(value)


def calculator(expr: str) -> str:
    """算一个纯算术表达式。只支持数字、括号和 + - * / // % **。

    三道闸，按代价从低到高排：
      1. 长度上限（解析前就挡，最便宜）
      2. 节点类型白名单（解析后走一遍，**先拒绝再求值**）
      3. 每一步结果的绝对值上限 + 指数上限（挡 `9**9**9` 这类算爆的）

    返回 `表达式 = 结果` 的形式，把算式一起回喂 —— 这样模型和轨迹里都能看见
    它到底算了什么，而不是只有一个孤零零的数字。
    """
    if len(expr) > CALC_MAX_CHARS:
        raise ToolRefused(f"表达式 {len(expr)} 字符，超过上限 {CALC_MAX_CHARS} 字符")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ToolRefused(f"表达式语法错误：{exc.msg}") from None

    for node in ast.walk(tree):
        if type(node) not in _ALLOWED_NODES:
            raise ToolRefused(
                f"表达式里不允许出现 {type(node).__name__}"
                "（只允许数字、括号和 + - * / // % **）"
            )

    return f"{expr} = {_format(_eval_node(tree.body))}"


def append_file(path: str, text: str) -> str:
    """把 text 追加写到 mini-agent 目录内的文件末尾；文件不存在就创建。

    ⚠️ 这是**第一个有副作用的工具**。有副作用意味着三件事都变了：

      1. **超时的语义变了。** 只读工具超时最坏是白读一遍；写工具超时是
         "**结果未知**" —— 它可能已经写成功了，只是我们没等到回话。
         所以 ToolSpec.side_effect 会改变超时提示语（见 _describe_failure）：
         对写工具，不能只说"失败了"，必须说"别盲目重试"。
      2. **它会被重复执行而效果叠加。** 同一条任务跑 N 次，文件里就累积 N 份。
         所以 T4 之前必须能把目标文件清掉（见 agent.py 的 reset_artifacts）。
         前八步所有任务都只读、跑一百次互不影响 —— 这个性质到此为止。
      3. **它更需要被边界挡住。** 越界与敏感文件都由 _resolve 统一拦下，
         这个工具一行安全代码都不用自己写。

    写入**逐字节照抄** text，不自动补换行：这样"写进去什么、读回来就是什么"，
    模型的写后自检才能真的对上。要换行就自己在 text 里带。
    """
    if len(text) > MAX_APPEND:
        # 用我们自己抛的异常，它的消息不含主机路径，可以安全透传给模型
        raise ToolRefused(f"拒绝写入：内容 {len(text)} 字符，超过上限 {MAX_APPEND} 字符")
    target = _resolve(path)
    if target.is_dir():
        raise IsADirectoryError(f"{path} 是目录，不是文件")

    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return (
        f"已追加写入 {path}：本次 {len(text)} 字符，"
        f"文件现在共 {target.stat().st_size} 字节"
    )


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
    # 这个工具会不会改变磁盘状态。默认 False（只读）。
    # 它不是给模型看的（schema() 不输出它），而是给我们自己看的 ——
    # 超时提示语、能不能重试、要不要重置，都要按这个字段分岔。
    side_effect: bool = False

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
    ToolSpec(
        name="list_dir",
        # description 是模型选不选这个工具的唯一依据。这里必须说清"什么时候用"，
        # 而不是只说"它是什么" —— 模型是按场景挑工具的。
        description=(
            "列出 mini-agent 目录内某个目录有哪些文件和子目录，"
            "返回每项的类型、字节数和名字（**不含文件内容**）。"
            "当不确定某个文件是否存在，或面对多个候选文件需要先挑选时，先用它。"
            "path 必须是相对 mini-agent 目录的路径，例如 'data'。"
        ),
        params={"path": "相对 mini-agent 目录的目录路径，例如 'data'"},
        required=("path",),
        func=list_dir,
    ),
    ToolSpec(
        name="calculator",
        # description 要回答"什么时候用它"，而不只是"它是什么"。
        # 这里刻意加了一句行为引导（"别自己心算"）—— description 就是提示词，
        # 见复盘 §7 Q3。
        description=(
            "计算一个纯算术表达式并返回结果。只支持数字、括号和 + - * / // % ** 运算。"
            "不做文件或网络访问，纯粹算数。"
            "需要精确算数（尤其是除法、百分比、多位数相乘）时用它，别自己心算。"
            "expr 例如 '(3800 - 1200) / 4'。"
        ),
        params={"expr": "要计算的算术表达式，例如 '(3800 - 1200) / 4'"},
        required=("expr",),
        func=calculator,
    ),
    ToolSpec(
        name="append_file",
        description=(
            "把一个字符串**追加**写到 mini-agent 目录内某个文件的末尾（文件不存在就创建）。"
            "这是唯一会改变磁盘的工具：写完之后请用 read_file 读回来确认写对了。"
            "每次调用都是追加，不会覆盖原有内容。"
            "path 必须是相对 mini-agent 目录的路径，例如 'logs/answers.txt'。"
        ),
        params={
            "path": "相对 mini-agent 目录的文件路径，例如 'logs/answers.txt'",
            "text": "要追加写入的内容，原样写入，不自动加换行",
        },
        required=("path", "text"),
        func=append_file,
        side_effect=True,
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


def _describe_failure(exc: Exception, name: str, args: object, spec: ToolSpec) -> str:
    """把异常翻译成一段**给模型看的**文本。

    白名单，不是清洗：只有认得出来的异常才带细节，而且那个细节是用
    **模型自己传进来的相对路径**拼的。认不出来就一句话打发 ——
    原始 message 一概不透传，因为它可能含主机绝对路径。

    需要 `spec` 是为了 ToolTimeout 那一条：同一个超时，在只读工具和
    有副作用的工具上**后果完全不同**，提示语也必须不同。
    """
    path = args.get("path") if isinstance(args, dict) else None
    at = f"：{path}" if isinstance(path, str) else ""

    if isinstance(exc, ToolTimeout):
        if spec.side_effect:
            # 写工具超时是"结果未知"，不是"失败"。这句话必须说清楚，
            # 否则模型会照最自然的直觉去重试，于是写两遍。
            return (
                f"工具 {name!r} 超时，超过 {exc.timeout:g} 秒仍未返回。"
                "⚠️ **结果未知**：它可能已经执行成功了，只是我们没等到回话。"
                "不要直接重试。先用只读工具（如 read_file）确认当前状态，再决定下一步。"
            )
        return (
            f"工具 {name!r} 超时，超过 {exc.timeout:g} 秒仍未返回，已放弃等待。"
            "可以换个做法，或如实说明这一步失败了。"
        )
    if isinstance(exc, ToolRefused):
        return str(exc)
    if isinstance(exc, PathEscape):  # 必须排在 PermissionError 前面
        return str(exc)
    if isinstance(exc, SensitivePath):  # 同上
        return str(exc)
    if isinstance(exc, RunTrace):  # 同上
        return str(exc)
    if isinstance(exc, FileNotFoundError):
        return f"文件不存在{at}（本项目路径都相对 mini-agent 目录写，不带盘符）"
    if isinstance(exc, IsADirectoryError):
        return f"{path} 是目录，不是文件，请给出文件路径"
    if isinstance(exc, NotADirectoryError):
        return f"这里不是目录{at}（也可能路径中间有一段是文件，不是目录）"
    if isinstance(exc, PermissionError):
        return f"读不了{at}（路径必须留在 mini-agent 目录内）"
    if isinstance(exc, UnicodeDecodeError):
        return f"{path} 不是 UTF-8 文本，读不成文本"
    if isinstance(exc, ZeroDivisionError):
        return "除数为零，算不出结果"
    if isinstance(exc, OverflowError):
        return "数值太大，超出可计算范围"
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
            f"ERROR: {_describe_failure(exc, name, args, spec)}", kind, type(exc).__name__
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
    ("敏感文件：凭据不给模型（第 9 步补的洞）", "read_file", {"path": ".env"}),
    ("敏感文件：模板也一起挡", "read_file", {"path": ".env.example"}),
    # --- 阶段 1 第 2 步：轨迹是仪器，不是任务输入（第三类拒绝）-------------
    (
        "读运行轨迹被拒（模型会去翻历史轨迹，第 9 步实测 4 次）",
        "read_file",
        {"path": "logs/run-20261013-212734.jsonl"},
    ),
    (
        "写运行轨迹被拒（轨迹是实验证据，不许覆盖）",
        "append_file",
        {"path": "logs/run-20261013-212734.jsonl", "text": "hack"},
    ),
    ("对文件调用 list_dir", "list_dir", {"path": "requirements.txt"}),
    ("append_file 少了 text", "append_file", {"path": "logs/_probe.txt"}),
    ("append_file 内容过长", "append_file", {"path": "logs/_probe.txt", "text": "x" * 3000}),
    ("append_file 想写 .env", "append_file", {"path": ".env", "text": "hack"}),
    # --- calculator：这个工具的价值之一，是**能主动构造失败** -------------
    ("calculator 语法错误", "calculator", {"expr": "1 +"}),
    ("calculator 除零", "calculator", {"expr": "1/0"}),
    ("calculator 整除零", "calculator", {"expr": "5//0"}),
    ("calculator 试图执行代码（白名单的核心用例）", "calculator",
     {"expr": "__import__('os').system('dir')"}),
    ("calculator 属性访问", "calculator", {"expr": "(1).__class__"}),
    ("calculator 变量名", "calculator", {"expr": "x + 1"}),
    ("calculator 字符串常量", "calculator", {"expr": "'abc'"}),
    ("calculator 布尔常量", "calculator", {"expr": "True"}),
    ("calculator 指数爆炸", "calculator", {"expr": "9**9**9"}),
    ("calculator 结果是复数", "calculator", {"expr": "(-1)**0.5"}),
    ("calculator 表达式过长", "calculator", {"expr": "1+" * 150 + "1"}),
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


def _slow_write_probe(text: str) -> str:
    """假装在写文件，但会卡住 —— 用来证明"写工具超时"的提示语必须与只读不同。"""
    time.sleep(5)
    return f"（自检假的）已写入 {text}"


_SLOW_WRITE_SPEC = ToolSpec(
    name="slow_write_probe",
    description="自检专用，不会发给模型",
    params={"text": "写什么"},
    required=("text",),
    func=_slow_write_probe,
    side_effect=True,  # ← 唯一区别
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
        print(f"  dispatch({name!r}, {str(args)[:72]})")
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

    print("\n=== 第 9 步：同样超时，有副作用的工具必须换说法 ===")
    _REGISTRY[_SLOW_WRITE_SPEC.name] = _SLOW_WRITE_SPEC
    out = dispatch("slow_write_probe", {"text": "x"}, timeout=0.5)
    seen.append(("写工具超时", out))
    print("  dispatch('slow_write_probe', {'text': 'x'}, timeout=0.5)")
    print(f"  -> {out}")
    print("  ^^^ 只读工具说'可以换条路'，写工具说'结果未知，别直接重试'。")
    print("      唯一的区别是 ToolSpec.side_effect —— 声明式的好处就在这里。")
    del _REGISTRY[_SLOW_WRITE_SPEC.name]

    print("\n=== append_file 正常路径（真写盘，跑完就删）===")
    probe = ROOT / "logs" / "_selftest_append.txt"
    probe.unlink(missing_ok=True)
    print("  " + dispatch("append_file", {"path": "logs/_selftest_append.txt", "text": "第一行\n"}))
    print("  " + dispatch("append_file", {"path": "logs/_selftest_append.txt", "text": "第二行\n"}))
    content = probe.read_text(encoding="utf-8")
    print(f"  读回来：{content!r}")
    assert content == "第一行\n第二行\n", "追加的结果不对"
    print("  两次调用的效果**叠**在一起 —— 这就是「有副作用」的字面含义：")
    print("  同样的调用做两次，世界不是原来的世界。文件已删除。")
    probe.unlink()

    print("\n=== 阶段 1 第 2 步：轨迹隔离 —— 判据是「任务书有没有要求」 ===")
    # 前置条件：_is_run_trace 靠 "target.parent == LOGS_DIR" 认轨迹，
    # 而 LOGS_DIR 来自 runlog.py、target 来自 tools.ROOT。两处若不同源，隔离静默失效。
    assert ROOT / "logs" == LOGS_DIR, (
        f"tools.ROOT/'logs'（{ROOT / 'logs'}）与 runlog.LOGS_DIR（{LOGS_DIR}）不同源 —— "
        "_is_run_trace 会认不出轨迹，且不会有任何报错"
    )
    print(f"  两处 logs 目录同源：{LOGS_DIR}")
    print("  必须挡的（上面 _FAILURE_CASES 里最后两条）：read_file / append_file 碰轨迹 → RunTrace")

    print("\n  必须放行的（同目录、非轨迹 —— 挡错了 T4 会静默失效）：")
    ans = LOGS_DIR / "answers.txt"
    made_ans = not ans.exists()
    if made_ans:
        ans.write_text("（自检临时写入）\n", encoding="utf-8")
    try:
        out = dispatch("read_file", {"path": "logs/answers.txt"})
        seen.append(("放行 answers.txt", out))
        print(f"    read_file('logs/answers.txt') -> {out.strip()[:40]!r}")
        assert not out.startswith("ERROR"), (
            f"logs/answers.txt 被挡住了 —— T4 要写它、再读回来校验，会静默失效：{out}"
        )
    finally:
        if made_ans:
            ans.unlink(missing_ok=True)

    print("\n  必须放行的（logs/ 之外的同名文件 —— 挡了就误伤普通文件）：")
    stray = ROOT / "run-99999999-999999.jsonl"
    stray.write_text("这不是轨迹，只是一段普通内容\n", encoding="utf-8")
    try:
        out = dispatch("read_file", {"path": stray.name})
        seen.append(("放行同名普通文件", out))
        print(f"    read_file('{stray.name}') -> {out.strip()[:40]!r}")
        assert not out.startswith("ERROR"), (
            f"logs/ 外的 run-*.jsonl 被误伤了 —— _is_run_trace 的目录限定没起作用：{out}"
        )
    finally:
        stray.unlink(missing_ok=True)

    print("\n  list_dir('logs') 也不许列出轨迹（光看目录名就能推出 T1/T4 的答案）：")
    listing = dispatch("list_dir", {"path": "logs"})
    seen.append(("list_dir logs", listing))
    for line in listing.splitlines():
        print(f"    {line}")
    assert ".jsonl" not in listing, (
        f"list_dir('logs') 列出了轨迹 —— 模型据此可直接答出 run-<时间戳>.jsonl：\n{listing}"
    )
    print("    没有出现任何 .jsonl —— 轨迹已从目录列表里消失。")

    print("\n=== calculator 正常路径（纯本地，带断言）===")
    expected = {
        "2+3*4": "2+3*4 = 14",
        "(3800 - 1200) / 4": "(3800 - 1200) / 4 = 650",
        "3800 * 7 / 15": "3800 * 7 / 15 = 1773.33",
        "2**10": "2**10 = 1024",
        "7 % 3": "7 % 3 = 1",
        "2.5 * 4": "2.5 * 4 = 10",
    }
    for expr, want in expected.items():
        got = dispatch("calculator", {"expr": expr})
        print(f"  {expr:<20} -> {got}")
        assert got == want, f"{expr}: 期望 {want!r}，得到 {got!r}"
    print("  6 条全部与手算一致。")

    print("\n=== 泄露自检：ERROR 文本里不许出现主机路径 ===")
    suspects = (str(ROOT), str(ROOT.parent), chr(92) * 2)  # chr(92) 就是反斜杠
    leaked = [(label, out) for label, out in seen if any(s in out for s in suspects)]
    for label, out in leaked:
        print(f"  [泄露] {label} -> {out}")
    if leaked:
        print(f"  失败：{len(leaked)} 条输出里出现了主机路径")
    else:
        print(f"  OK：{len(seen)} 条输出里没有一条含有主机路径或双反斜杠")

    print("\n=== 密钥自检：任何观察文本都不许带出 .env 的内容 ===")
    key_hits = [(label, out) for label, out in seen if "OPENAI_API_KEY" in out or "=sk" in out]
    for label, out in key_hits:
        print(f"  [泄露] {label} -> {out[:80]}")
    if key_hits:
        print(f"  失败：{len(key_hits)} 条输出带出了凭据内容")
    else:
        print("  OK：没有一条输出包含 OPENAI_API_KEY 或 =sk")

    print("\n=== 正常路径对照 ===")
    out = dispatch("read_file", {"path": "requirements.txt"})
    print("  dispatch('read_file', {'path': 'requirements.txt'}) 前 60 字符：")
    print(f"  -> {out[:60]!r}")
    print("\n注意每一行都以 ERROR 开头且**没有抛出异常** —— 这就是 dispatch 存在的意义。")


if __name__ == "__main__":
    _self_test()
