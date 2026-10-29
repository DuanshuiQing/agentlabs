"""M0 · go/no-go 探针：在写任何 agent 代码之前，先把"环境到底能不能干这件事"问清楚。

为什么先写这个：本工作区已经踩过 5 个"钱花完了才发现环境不支持"的坑
（沙箱里 pip 装不成、临时目录写不进、MSYS 程序建不出命名管道……）。
这个项目的**执行通道是子进程**，而本机的 DSH 沙箱对进程与管道有历史限制。
所以先花半小时把下面的问题逐个问一遍，任一项 FAIL 就当场改设计，别等写完 1000 行才发现。

它测的是**环境事实**，不是代码质量；6 类拒绝的自检在 M1 的 `tools.py::_self_test` 里
（那时才有 `_resolve` 这个对象可测）。

五项检查：

  1. 依赖          openai / python-dotenv 可导入（用哪个解释器、什么版本）
  2. 子进程        `subprocess.run(..., capture_output=True)` 能否拿到 stdout/stderr
                   —— 这是 `run_tests` 的基础。不行则全项目改「重定向到文件再读」
  3. git 与副本    copytree + `git init/add/commit` 能否跑通（沙箱副本与 patch 归档靠它）
                   顺带验证「改写后 `git checkout -- .` 能还原」——判定协议靠它
  4. unittest      纯标准库跑套件：全绿 → 退出码 0；有失败 → 退出码 1 且输出可解析
                   —— 判定器要解析的正是这两种输出
  5. 落盘规矩      `write_text(newline="\n")` 不产生 CRLF、`ast.parse` 可用、目录能否删除
  6. 模型          一次真实小调用（约 20 tokens），确认密钥与网络可用

运行：

    mini-agent\\.venv\\Scripts\\python.exe swe-agent\\probe_env.py

退出码 0 = 全部 PASS（INFO 不算失败）；1 = 有 FAIL，先改设计再往下写。
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from fsutil import rmtree_force  # noqa: E402 —— 必须先把 HERE 塞进 sys.path

MINI = HERE.parent / "mini-agent"
PROBE = HERE / "sandbox" / "_probe"

RESULTS: list[tuple[str, str, str]] = []  # (状态, 名字, 说明)


def record(status: str, name: str, detail: str) -> None:
    RESULTS.append((status, name, detail))
    print(f"  [{status:4}] {name:<22} {detail}")


def check(name: str):
    """把一个可能抛异常的检查包成一条结果。FAIL 记下异常类型，不中断后续检查。"""

    def deco(fn):
        def wrapper() -> None:
            t0 = time.perf_counter()
            try:
                status, detail = fn()
            except Exception as exc:  # noqa: BLE001 —— 探针要把失败原样摊开
                record("FAIL", name, f"{type(exc).__name__}: {exc}")
                return
            cost = f"  ({time.perf_counter() - t0:.2f}s)"
            record(status, name, str(detail) + cost)

        return wrapper

    return deco


# ---------------------------------------------------------------------------
# 1. 依赖
# ---------------------------------------------------------------------------
@check("依赖导入")
def dep_check() -> tuple[str, str]:
    import dotenv  # noqa: F401
    import openai

    return "PASS", f"python={sys.version.split()[0]} openai={openai.__version__}"


# ---------------------------------------------------------------------------
# 2. 子进程管道捕获 —— run_tests 的命门
# ---------------------------------------------------------------------------
def child_env() -> dict:
    """子进程环境白名单 —— `run_tests` 也会用同一份。

    两个成分缺一不可：
      · 白名单：**不把 OPENAI_API_KEY 递进 agent 能影响的进程**；
      · UTF-8 开关：子进程的 stdout 是管道时，Python 会退回系统 locale（本机 GBK），
        父进程按 UTF-8 解码就得到乱码（本探针第一次跑就撞上了，对应 README 坑 2）。
    """
    keep = (
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
    )
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env["PYTHONIOENCODING"] = "utf-8"
    # 故意不设 PYTHONUTF8=1：它会让 open() 的默认编码也变成 UTF-8，
    # 从而**掩盖**"忘了写 encoding='utf-8'"这类真 bug（S04 测的就是它）。
    return env


@check("子进程捕获")
def subprocess_check() -> tuple[str, str]:
    code = (
        "import sys\n"
        "print('stdout-线')\n"
        "print('stderr-线', file=sys.stderr)\n"
        "sys.exit(3)\n"
    )
    # 先跑一次**不设** UTF-8 的，把根因量出来（这是本机的真实行为，不是猜测）
    naive = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    gbk = "stdout-线" not in naive.stdout
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        env=child_env(),
    )
    if "stdout-线" not in proc.stdout:
        return "FAIL", f"没拿到 stdout，实际={proc.stdout!r}"
    if "stderr-线" not in proc.stderr:
        return "FAIL", f"没拿到 stderr，实际={proc.stderr!r}"
    if proc.returncode != 3:
        return "FAIL", f"退出码应 3，实际 {proc.returncode}"
    note = "（默认 env 下中文确实退回 GBK，已量到）" if gbk else "（默认 env 也是 UTF-8）"
    return "PASS", f"stdout/stderr/退出码 三者都对{note}"


# ---------------------------------------------------------------------------
# 3. git 与副本 —— 沙箱重置、patch 归档、判定还原都靠它
# ---------------------------------------------------------------------------
def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=swe-agent",
            "-c",
            "user.email=swe-agent@local",
            "-C",
            str(repo),
            *args,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


@check("git 与副本")
def git_check() -> tuple[str, str]:
    rmtree_force(PROBE)
    src = PROBE / "src"
    (src / "pkg").mkdir(parents=True)
    (src / "pkg" / "core.py").write_text("def add(a, b):\n    return a + b\n", newline="\n")

    dst = PROBE / "repo"
    shutil.copytree(src, dst)

    for args, what in (
        (("init", "-q"), "init"),
        (("add", "-A"), "add"),
        (("commit", "-qm", "baseline"), "commit"),
    ):
        proc = _git(dst, *args)
        if proc.returncode != 0:
            return "FAIL", f"git {what} 失败: {(proc.stderr or proc.stdout).strip()[:160]}"

    if _git(dst, "status", "--porcelain").stdout.strip():
        return "FAIL", "基线 commit 后 status 不干净"

    target = dst / "pkg" / "core.py"
    target.write_text("def add(a, b):\n    return a - b  # 播种\n", newline="\n")
    diff = _git(dst, "diff").stdout
    if "return a - b" not in diff:
        return "FAIL", "git diff 没看到改动"
    if "pkg/core.py" not in diff:
        return "FAIL", "git diff 没带文件名"

    _git(dst, "checkout", "--", ".")
    if "return a + b" not in target.read_text(encoding="utf-8"):
        return "FAIL", "git checkout -- . 没能还原"

    return "PASS", f"copytree/init/commit/diff/checkout 全通，diff {len(diff)} 字符"


# ---------------------------------------------------------------------------
# 4. unittest —— 纯标准库，判定器要解析的正是这两种输出
# ---------------------------------------------------------------------------
@check("unittest 套件")
def unittest_check() -> tuple[str, str]:
    repo = PROBE / "ut"
    rmtree_force(repo)
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("", newline="\n")
    (repo / "pkg" / "core.py").write_text(
        "def add(a, b):\n    return a + b\n", newline="\n"
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "__init__.py").write_text("", newline="\n")
    test = repo / "tests" / "test_core.py"
    test.write_text(
        "import unittest\n"
        "from pkg.core import add\n"
        "\n"
        "class TestAdd(unittest.TestCase):\n"
        "    def test_ok(self):\n"
        "        self.assertEqual(add(1, 2), 3)\n"
        "\n"
        "    def test_bug(self):\n"
        "        self.assertEqual(add(2, 2), 5)\n",
        newline="\n",
    )

    def run() -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-v"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            env=child_env(),
        )

    green = run()  # 有 1 条故意失败
    if green.returncode == 0:
        return "FAIL", "有失败测试却返回 0，退出码不可信"
    failing = (green.stderr or "") + (green.stdout or "")
    if "test_bug" not in failing or "FAILED" not in failing:
        return "FAIL", f"失败输出无法解析（找不到 test_bug/FAILED）: {failing[-120:]!r}"

    test.write_text(
        "import unittest\n"
        "from pkg.core import add\n"
        "\n"
        "class TestAdd(unittest.TestCase):\n"
        "    def test_ok(self):\n"
        "        self.assertEqual(add(1, 2), 3)\n",
        newline="\n",
    )
    ok = run()
    if ok.returncode != 0:
        return "FAIL", f"改绿后退出码应 0，实际 {ok.returncode}: {(ok.stderr or '')[-120:]!r}"

    # 把解析所需的两种形状留一份样本给 eval/judge.py
    sample = failing.strip().splitlines()
    return "PASS", f"红=退出码 {green.returncode}，绿=退出码 {ok.returncode}；样本 {len(sample)} 行"


# ---------------------------------------------------------------------------
# 5. 落盘规矩（LF / ast / 目录删除）
# ---------------------------------------------------------------------------
@check("LF 与 ast")
def write_check() -> tuple[str, str]:
    f = PROBE / "line.txt"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("a\nb\n", newline="\n")
    raw = f.read_bytes()
    if b"\r\n" in raw:
        return "FAIL", "write_text(newline='\\n') 仍写出 CRLF"
    tree = ast.parse("x = 1\n")
    if not isinstance(tree, ast.Module):
        return "FAIL", "ast.parse 返回类型不对"
    return "PASS", f"bytes={raw!r}，ast 可用（{len(ast.dump(tree))} 字符）"


@check("删除含 .git 的副本")
def rmtree_check() -> tuple[str, str]:
    """真正的重启场景：下一次运行前要把带 `.git` 的旧副本清掉。

    第一次跑探针时这一步失败了（git 把 `.git/objects/**` 设成 ReadOnly，
    `shutil.rmtree` 删不掉），而 `ignore_errors=True` 把失败咽掉，
    下一次 `copytree` 才报 WinError 183 —— 错误出现在"建"，根因在"删"。
    所以这里断言的是 `fsutil.rmtree_force` 的返回值，不是"没抛异常"。
    """
    victim = PROBE / "repo"  # git_check 刚建的那个，带 .git
    if not victim.exists():
        return "INFO", "没有可删的带 .git 副本（git_check 没跑到）"
    ok = rmtree_force(victim)
    if not ok:
        return "FAIL", "带 .git 的副本删不掉 —— 只能用 git 重置，或在你自己的窗口里删"
    return "PASS", "含 .git 的副本可删（先摘只读位再删）"


# ---------------------------------------------------------------------------
# 6. 一次真实小调用
# ---------------------------------------------------------------------------
def _api_env() -> tuple[dict, str]:
    """读密钥：先 swe-agent/.env，没有就读 ../mini-agent/.env（不另存一份明文）。"""
    from dotenv import dotenv_values

    for cand in (HERE / ".env", MINI / ".env"):
        if cand.exists():
            vals = {k: v for k, v in dotenv_values(cand).items() if v}
            return vals, str(cand.relative_to(HERE.parent))
    raise FileNotFoundError("既没有 swe-agent/.env，也没有 mini-agent/.env")


@check("模型调用")
def model_check() -> tuple[str, str]:
    from openai import OpenAI

    env, where = _api_env()
    key = (env.get("OPENAI_API_KEY") or "").strip()
    if not key:
        return "FAIL", f"{where} 里没有 OPENAI_API_KEY"
    model = (env.get("MODEL") or "").strip()
    if not model:
        return "FAIL", f"{where} 里没有 MODEL"
    client = OpenAI(
        api_key=key,
        base_url=(env.get("OPENAI_BASE_URL") or "").strip() or None,
        timeout=60.0,
    )
    resp = client.chat.completions.create(
        model=model, messages=[{"role": "user", "content": "只回复两个字：就绪"}]
    )
    text = (resp.choices[0].message.content or "").strip()
    usage = getattr(resp, "usage", None)
    spent = f"，{usage.total_tokens} tokens" if usage else ""
    return "PASS", f"model={model}（env 来自 {where}）回复={text!r}{spent}"


def main() -> int:
    print("=" * 72)
    print("swe-agent M0 探针")
    print(f"解释器 : {sys.executable}")
    print(f"工作目录: {HERE}")
    print("=" * 72)
    for fn in (
        dep_check,
        subprocess_check,
        git_check,
        unittest_check,
        write_check,
        rmtree_check,
        model_check,
    ):
        fn()

    fails = [r for r in RESULTS if r[0] == "FAIL"]
    infos = [r for r in RESULTS if r[0] == "INFO"]
    print("-" * 72)
    print(f"PASS {len(RESULTS) - len(fails) - len(infos)} / INFO {len(infos)} / FAIL {len(fails)}")
    if fails:
        print("\n先把这些解决，再动 agent 代码：")
        for _, name, detail in fails:
            print(f"  - {name}: {detail}")
        return 1
    print("\n结论：环境支持本项目的执行通道（子进程 + 套件解析 + git 副本）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
